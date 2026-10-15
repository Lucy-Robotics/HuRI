import asyncio
import os
import queue
import sys
import traceback
from typing import AsyncGenerator, Optional

import numpy as np
from huri_events.audio import Audio
from ray import serve

# Defaults — overridden by env vars in production (see README.md)
_MODEL_PATH = os.environ.get(
    "HURI_MODEL_PATH", "/models/cosytts/FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
)
_VOICE_SAMPLE_PATH = os.environ.get("HURI_VOICE_SAMPLE_PATH", "/assets/voice.wav")
_DEFAULT_INSTRUCTION = "You are a helpful assistant."


def _normalize_transcript(raw: str) -> str:
    """Make a reference transcript safe for CosyVoice3.

    CosyVoice3 expects "<instruction><|endofprompt|><transcript-of-voice.wav>".
    If the configured transcript supplies a bare transcript (no marker), prepend
    the default instruction so the transcript lands AFTER <|endofprompt|> —
    otherwise the LM treats it as an instruction and intermittently renders it as
    speech (prompt leakage).
    """
    return (
        raw
        if "<|endofprompt|>" in raw
        else f"{_DEFAULT_INSTRUCTION}<|endofprompt|>{raw}"
    )


_END_TEXT = object()  # sentinel pushed into the text queue to close synth
_END_AUDIO = object()  # sentinel pushed into the audio queue when synth completes
_DONE = object()  # sentinel for exhausted sync generator


@serve.deployment(name="CosyVoiceTTSHandle", max_ongoing_requests=200)
class CosyVoiceTTSHandle:
    """CosyVoice3 wrapper with per-session bistream synthesis.

    The model's `inference_zero_shot` accepts a Python generator as `tts_text`
    and yields audio chunks as text arrives — that's the "bistream" mode.
    Because the model call is fully synchronous, each session runs in a thread
    via `run_in_executor` and is fed by a thread-safe `queue.Queue` that the
    asyncio side pushes text into.
    """

    def __init__(
        self,
        model_path: str = _MODEL_PATH,
        voice_sample_path: str = _VOICE_SAMPLE_PATH,
        voice_sample_transcript: Optional[str] = None,
    ):
        cosy_dir = os.environ.get("HURI_COSY_DIR")
        if cosy_dir:
            matcha_path = os.path.join(cosy_dir, "third_party", "Matcha-TTS")
            if os.path.isdir(matcha_path) and matcha_path not in sys.path:
                sys.path.insert(0, matcha_path)

        from cosyvoice.cli.cosyvoice import CosyVoice3

        # Resolve the reference transcript here (deploy time on the GPU worker)
        # rather than at module import: importing this module must not require
        # HURI_VOICE_TRANSCRIPT, since modules.py imports it inside a broad
        # try/except that would otherwise make TTS silently vanish from the
        # pipeline when the var is unset. Fail loudly and locally instead.
        if voice_sample_transcript is None:
            raw = os.environ.get("HURI_VOICE_TRANSCRIPT")
            if not raw:
                raise RuntimeError(
                    "HURI_VOICE_TRANSCRIPT is not set. The TTS deployment needs the "
                    "transcript of the reference voice sample (voice.wav). Set it in "
                    "the Serve app runtime_env.env_vars (see deploy values.yaml)."
                )
            voice_sample_transcript = raw
        voice_sample_transcript = _normalize_transcript(voice_sample_transcript)

        # fp16 is the whole point of running on a bandwidth-rich GPU (e.g. V100):
        # it routes CosyVoice's LM/flow/vocoder matmuls through the fp16 tensor
        # cores and halves memory traffic. CosyVoice3 defaults fp16=False (fp32),
        # which leaves that advantage entirely unused. Default ON when a CUDA
        # device is present; override with HURI_TTS_FP16 (fp16 needs CUDA, so it
        # is forced off on CPU regardless).
        import torch

        _fp16_env = os.environ.get("HURI_TTS_FP16")
        if _fp16_env is not None:
            fp16 = _fp16_env.strip().lower() in ("1", "true", "yes", "on")
        else:
            fp16 = torch.cuda.is_available()
        if fp16 and not torch.cuda.is_available():
            print("[TTS] fp16 requested but no CUDA device; falling back to fp32")
            fp16 = False

        # TensorRT accelerates the flow-matching estimator (often the single
        # biggest TTS speedup) but builds a device-specific engine at startup
        # (~minutes) and needs validation per GPU arch. OFF by default; flip
        # HURI_TTS_TRT=1 to experiment once fp16 is confirmed working.
        load_trt = os.environ.get("HURI_TTS_TRT", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        print(
            f"[TTS] loading CosyVoice3 (fp16={fp16}, load_trt={load_trt}) "
            f"from {model_path!r}"
        )
        self.model = CosyVoice3(model_dir=model_path, load_trt=load_trt, fp16=fp16)
        self.sample_rate: int = self.model.sample_rate

        self.prompt_speech = voice_sample_path
        self.prompt_text: str = voice_sample_transcript

        self._text_queues: dict[str, queue.Queue] = {}

        # Pay CosyVoice's first-synth costs now, at deploy time, rather than on
        # the first user utterance (see _warmup).
        self._warmup()

    def _warmup(self) -> None:
        """Run one throwaway synthesis so the first real utterance is hot.

        The first CosyVoice call pays one-time costs — CUDA context init,
        cuBLAS/cuDNN algorithm selection, the LM/flow/vocoder first-call
        compiles, and the caching allocator's first growth — that otherwise land
        on the first user utterance and stall it for seconds. Draining a dummy
        synth through the *real* fp16 path (same prompt, stream=True) pays them
        upfront, mirroring the MOV module's warmup.

        Best-effort: never fatal. The reference sample (voice.wav) is uploaded to
        its PVC *after* the worker starts, so on a brand-new volume it may be
        absent on first boot — warmup is skipped then and runs on the next
        restart once the PVC holds the file.
        """
        import time

        if not os.path.isfile(self.prompt_speech):
            print(
                f"[TTS] warmup skipped: reference sample {self.prompt_speech!r} "
                "not present yet (upload voice.wav, then restart to warm)"
            )
            return
        try:
            t0 = time.time()

            def _dummy_text():
                yield "Hello, this is a warm up."

            audio_iter = self.model.inference_zero_shot(
                _dummy_text(),
                self.prompt_text,
                self.prompt_speech,
                stream=True,
            )
            chunks = sum(1 for _ in audio_iter)
            print(f"[TTS] warmup done ({chunks} chunks) in {time.time() - t0:.2f}s")
        except Exception as e:  # noqa: BLE001 — warmup is an optimisation, never fatal
            print(f"[TTS] WARNING warmup failed: {e!r}")

    async def get_sample_rate(self) -> int:
        return self.sample_rate

    async def start_session(self, session_id: str) -> None:
        self._text_queues[session_id] = queue.Queue()

    async def push_text(self, session_id: str, text: str, end: bool) -> None:
        q = self._text_queues.get(session_id)
        if q is None:
            return
        if text:
            q.put(text)
        if end:
            q.put(_END_TEXT)

    async def stream_audio(self, session_id: str) -> AsyncGenerator[Audio, None]:
        text_q = self._text_queues[session_id]
        loop = asyncio.get_running_loop()
        chunk_count = 0

        def text_gen():
            while True:
                item = text_q.get()
                if item is _END_TEXT:
                    return
                yield item

        try:
            audio_iter = self.model.inference_zero_shot(
                text_gen(),
                self.prompt_text,
                self.prompt_speech,
                stream=True,
            )
            while True:
                result = await loop.run_in_executor(None, next, audio_iter, _DONE)
                if result is _DONE:
                    break
                assert isinstance(result, dict)
                chunk_count += 1
                speech = result["tts_speech"].squeeze(0).numpy().astype(np.float32)
                yield Audio(data=speech, sample_rate=self.sample_rate)
        except Exception:
            traceback.print_exc()
            raise
        finally:
            self._text_queues.pop(session_id, None)
