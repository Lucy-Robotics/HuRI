"""Piper text-to-speech deployment — the portable TTS engine.

Why this exists alongside CosyVoice3 (``text_to_speech.py``):

CosyVoice gives zero-shot voice cloning but needs torch plus a working
CUDA/ROCm stack, and on CPU it synthesises at roughly 7x *slower* than
realtime — unusable in a conversation. It also cannot run on consumer RDNA3
GPUs at all (the HiFi-GAN vocoder faults inside a MIOpen convolution), so on
an AMD machine there is no fast path.

Piper is ONNX-only: no torch, no GPU, a 61 MB voice model, and measured at
~0.03x realtime on a Ryzen 7840HS — about 30x faster than it speaks, with the
first chunk in ~30 ms. That makes it the engine that works on *any* machine,
which is why it is the default. CosyVoice stays selectable with
``HURI_TTS_ENGINE=cosyvoice`` when an NVIDIA GPU is present and the cloned
voice matters more than latency.

The public surface is deliberately identical to ``TTSHandle`` —
``get_sample_rate`` / ``start_session`` / ``push_text`` / ``stream_audio`` — so
``TTS`` (the per-session module) and the generated Serve config work with
either engine unchanged. Both are registered as ``name="TTSHandle"`` so a config's
``deployments: - name: TTSHandle`` block matches whichever is bound.
"""

import asyncio
import os
import re
import traceback
from typing import AsyncGenerator, Dict, List

import numpy as np
from ray import serve

from .events import Audio

_VOICE_PATH = os.environ.get(
    "HURI_PIPER_VOICE", "assets/models/piper/en_US-lessac-medium.onnx"
)
# Sentence-ish boundaries. Piper synthesises a whole utterance at a time, so we
# flush on these to start speaking before the LLM has finished writing.
_BOUNDARY = re.compile(r"[.!?…]['\"\)\]]?\s")
# Don't flush on a boundary this early — "Dr." or "1." would cut the phrase into
# unnatural fragments, and Piper's prosody is better with more context.
_MIN_FLUSH_CHARS = 24


@serve.deployment(name="TTSHandle")
class PiperTTSHandle:
    """Piper wrapper with per-session incremental synthesis.

    Stateless across sessions apart from the per-session text buffer: the ONNX
    session is shared and thread-safe for inference, so one replica serves every
    client.
    """

    def __init__(
        self,
        voice_path: str = _VOICE_PATH,
        length_scale: float | None = None,
        volume: float | None = None,
    ):
        from piper import PiperVoice

        if not os.path.isfile(voice_path):
            raise RuntimeError(
                f"[PiperTTS] voice model not found: {voice_path!r}. "
                "Download one with: python -m piper.download_voices "
                "--download-dir assets/models/piper en_US-lessac-medium"
            )

        self._voice = PiperVoice.load(voice_path)
        self._syn_config = self._build_syn_config(length_scale, volume)
        # Probe the real output rate rather than trusting a constant: it differs
        # per voice (22.05 kHz for -medium, 16 kHz for some -low voices) and the
        # client resamples against whatever we report.
        self._sample_rate = int(self._voice.config.sample_rate)
        self._buffers: Dict[str, List[str]] = {}
        self._queues: Dict[str, asyncio.Queue] = {}
        print(
            f"[PiperTTS] loaded {os.path.basename(voice_path)} "
            f"@ {self._sample_rate}Hz (onnxruntime, CPU)"
        )

    @staticmethod
    def _build_syn_config(length_scale: float | None, volume: float | None):
        """None when nothing is overridden, so Piper uses the voice's own defaults."""

        from piper import SynthesisConfig

        if length_scale is None and volume is None:
            return None
        # Fields set individually rather than via **kwargs: SynthesisConfig mixes
        # int/bool/float fields, so a kwargs dict cannot be type-checked.
        syn_config = SynthesisConfig()
        if length_scale is not None:
            syn_config.length_scale = length_scale
        if volume is not None:
            syn_config.volume = volume
        return syn_config

    async def get_sample_rate(self) -> int:
        return self._sample_rate

    async def start_session(self, session_id: str) -> None:
        self._buffers[session_id] = []
        self._queues[session_id] = asyncio.Queue()

    async def push_text(self, session_id: str, text: str, end: bool) -> None:
        """Accumulate LLM tokens, flushing complete phrases for synthesis.

        Unlike CosyVoice there is no bistream mode to push tokens into, so the
        phrase boundary is ours to choose. Flushing per sentence keeps the
        time-to-first-audio low while still giving Piper enough context for
        sensible prosody.
        """
        buf = self._buffers.setdefault(session_id, [])
        queue = self._queues.setdefault(session_id, asyncio.Queue())
        if text:
            buf.append(text)

        joined = "".join(buf)
        if not end:
            # Only flush on a boundary that is followed by whitespace, so a
            # decimal point or an abbreviation does not split the phrase.
            match = None
            for m in _BOUNDARY.finditer(joined):
                match = m
            if match is None or match.end() < _MIN_FLUSH_CHARS:
                return
            phrase, remainder = joined[: match.end()], joined[match.end() :]
            buf.clear()
            if remainder:
                buf.append(remainder)
        else:
            phrase, _ = joined, ""
            buf.clear()

        phrase = phrase.strip()
        if phrase:
            await queue.put(phrase)
        if end:
            await queue.put(None)  # end-of-utterance sentinel

    async def stream_audio(self, session_id: str) -> AsyncGenerator[Audio, None]:
        queue = self._queues.setdefault(session_id, asyncio.Queue())
        loop = asyncio.get_running_loop()
        pts = 0.0
        try:
            while True:
                phrase = await queue.get()
                if phrase is None:
                    # Zero-length end marker, matching TTSHandle's contract.
                    yield Audio(
                        data=np.zeros(0, dtype=np.float32),
                        sample_rate=self._sample_rate,
                        end=True,
                        pts=pts,
                    )
                    return
                # Synthesis is CPU-bound C++ inside onnxruntime; run it off the
                # event loop so the replica keeps serving other sessions.
                chunks = await loop.run_in_executor(None, self._synthesize, phrase)
                for samples in chunks:
                    if samples.size == 0:
                        continue
                    yield Audio(
                        data=samples,
                        sample_rate=self._sample_rate,
                        end=False,
                        pts=pts,
                    )
                    pts += samples.size / self._sample_rate
        except Exception:
            traceback.print_exc()
            raise
        finally:
            self._buffers.pop(session_id, None)
            self._queues.pop(session_id, None)

    def _synthesize(self, phrase: str) -> List[np.ndarray]:
        out: List[np.ndarray] = []
        for chunk in self._voice.synthesize(phrase, syn_config=self._syn_config):
            out.append(np.asarray(chunk.audio_float_array, dtype=np.float32))
        return out
