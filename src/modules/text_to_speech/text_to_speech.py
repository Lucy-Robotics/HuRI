import asyncio
import os
import queue
import sys
import traceback
import uuid
from typing import Any, AsyncGenerator, Optional

import numpy as np
from ray import serve
from ray.serve import handle

from src.core.module import ModuleWithHandle

from .events import Audio, Token

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


@serve.deployment(name="TTSHandle", max_ongoing_requests=200)
class TTSHandle:
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


def _select_engine():
    """Pick the TTS engine from HURI_TTS_ENGINE (default: piper).

    Both engines are registered as Serve deployment ``name="TTSHandle"`` and expose
    the same four methods, so a config's ``deployments: - name: TTSHandle`` block and
    the ``TTS`` module below work with either one unchanged.

    piper (default) is ONNX-only and ~30x faster than realtime on CPU, so it
    works on every machine. cosyvoice adds zero-shot voice cloning but needs
    torch plus CUDA — see piper_tts.py for the measurements behind this default.
    """

    engine = os.environ.get("HURI_TTS_ENGINE", "piper").strip().lower()
    if engine == "cosyvoice":
        return TTSHandle
    if engine in ("", "piper"):
        from .piper_tts import PiperTTSHandle

        return PiperTTSHandle
    raise ValueError(
        f"unknown HURI_TTS_ENGINE={engine!r}; expected 'piper' or 'cosyvoice'"
    )


class TTS(ModuleWithHandle):
    """TTS Module — bistream tokens-in / audio-out via CosyVoice3.

    Opens one synthesis session per utterance (delimited by `token.end`). Each
    incoming token is pushed straight into the model's text generator so audio
    starts coming back before the LLM has finished producing the response.
    No clause buffering on our side — CosyVoice's frontend handles segmentation
    and stitches LM calls together across the whole utterance.

    input: token (Token)
    output: audio (Audio)
    """

    _handle_cls = _select_engine()
    input_type = "token"
    output_type = "audio.out"

    def __init__(self, _handle: handle.DeploymentHandle):
        super().__init__(_handle)
        self._session_id: str | None = None
        self._audio_q: asyncio.Queue | None = None
        self._stream_task: asyncio.Task | None = None
        # The EventGraph fans each token out as its own concurrent process()
        # task on this shared instance. This lock serialises session setup and
        # text pushes so tokens reach CosyVoice's text queue in arrival order,
        # exactly once. asyncio.Lock wakes waiters FIFO and tokens are created
        # in order, so order is preserved — crucially the end-of-utterance token
        # can no longer overtake a content token (which would truncate synthesis
        # and silently drop trailing words).
        self._push_lock = asyncio.Lock()

    async def process(self, token: Token) -> AsyncGenerator[Audio, None]:
        # Acquire BEFORE any await so lock-acquisition order matches token order.
        # Setup + push happen under the lock; only the first token of an
        # utterance goes on to drain/yield audio (outside the lock, so pushes of
        # later tokens are never blocked by the long-running drain).
        async with self._push_lock:
            is_first = self._session_id is None
            if is_first:
                self._session_id = str(uuid.uuid4())
                self._audio_q = asyncio.Queue()
                print(
                    f"[TTS-client] [{self._session_id}] opening new utterance session"
                )
                await self._handle.start_session.remote(self._session_id)
                self._stream_task = asyncio.create_task(
                    self._drain_audio(self._session_id, self._audio_q)
                )

            sid = self._session_id
            audio_q = self._audio_q
            stream_task = self._stream_task
            print(f"[TTS-client] [{sid}] push token: {token.text!r} (end={token.end})")
            await self._handle.push_text.remote(sid, token.text, token.end)

        if not is_first:
            return

        assert audio_q is not None and stream_task is not None
        try:
            count = 0
            while True:
                item = await audio_q.get()
                if item is _END_AUDIO:
                    break
                count += 1
                print(f"[TTS-client] [{sid}] yield chunk #{count}")
                yield item
            await stream_task
            print(f"[TTS-client] [{sid}] utterance complete ({count} chunks)")

            sample_rate = await self._handle.get_sample_rate.remote()
            yield Audio(
                data=np.array([], dtype=np.float32), sample_rate=sample_rate, end=True
            )
        finally:
            async with self._push_lock:
                self._session_id = None
                self._audio_q = None
                self._stream_task = None

    async def _drain_audio(self, session_id: str, audio_q: asyncio.Queue) -> None:
        try:
            # Any: the Ray response type differs with/without ray installed
            # (CI lints without it), so a `type: ignore` would flip between
            # needed and unused.
            response: Any = self._handle.options(stream=True).stream_audio.remote(
                session_id
            )
            count = 0
            pts = 0.0
            async for audio in response:
                count += 1
                audio.pts = pts
                pts += audio.data.shape[0] / audio.sample_rate
                print(
                    f"[TTS-client] [{session_id}] drain received chunk #{count} "
                    f"pts={audio.pts:.3f}s next={pts:.3f}s",
                )
                await audio_q.put(audio)
        except Exception as e:
            print(f"[TTS-client] [{session_id}] drain task FAILED: {e!r}")
            raise
        finally:
            await audio_q.put(_END_AUDIO)
            print(f"[TTS-client] [{session_id}] drain task finished")
