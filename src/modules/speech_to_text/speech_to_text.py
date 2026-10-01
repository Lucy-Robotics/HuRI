import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, List

import numpy as np
from ray import serve
from ray.serve import handle

from src.core.module import ModuleWithHandle

from .events import Transcript, Voice

_MODEL_PATH = os.environ.get("HURI_STT_MODEL_PATH", "base")
_NUM_WORKERS = int(os.environ.get("HURI_STT_NUM_WORKERS", "2"))
# Set by the generated Serve config (scripts/install_local.sh) from the install
# plan's STT decision, already translated into faster-whisper's vocabulary:
# "cpu" | "cuda" | "auto" for the device, "int8"/"float16"/"auto" for the type.
# Defaulting to "auto" here keeps the hand-written configs in config/ working.
_DEVICE = os.environ.get("HURI_STT_DEVICE", "auto")
_COMPUTE_TYPE = os.environ.get("HURI_STT_COMPUTE_TYPE", "auto")


@serve.deployment(name="STTHandle", max_ongoing_requests=8)
class STTHandle:
    """faster-whisper model wrapper.

    Holds the WhisperModel and runs transcription on its own Ray Serve actor,
    off the HuRI master actor — model load and GPU inference no longer block the
    websocket ingress / per-session router. Pinned to a GPU worker via
    ray_actor_options in the Serve config (see deploy values.yaml).

    Stateless across calls: the per-session sliding-window buffering lives in the
    STT module, so this deployment is shared across all sessions.

    :model: path to (or size name of) the faster-whisper model. Defaults to the
        HURI_STT_MODEL_PATH env var, falling back to "base".
    :device: "cpu", "cuda", or "auto".
    :compute_type: e.g. "int8", "float16", or "auto".
    """

    def __init__(
        self,
        model: str = _MODEL_PATH,
        device: str = _DEVICE,
        compute_type: str = _COMPUTE_TYPE,
        num_workers: int = _NUM_WORKERS,
    ):
        from faster_whisper import WhisperModel

        # num_workers lets CTranslate2 service several transcriptions at once on
        # this single replica — each "worker" is an independent inference slot
        # over the shared (read-only) weights. Combined with the thread pool
        # below, N sessions are transcribed concurrently while staying fully
        # independent: no per-call client state is ever held here (the sliding
        # window lives in the per-session STT module).
        self.model_faster = WhisperModel(
            model,
            device=device,
            compute_type=compute_type,
            num_workers=num_workers,
        )
        # Run the *blocking* faster-whisper call off the actor's asyncio loop.
        # transcribe() used to be an async method that called the synchronous
        # model inline, blocking the replica's event loop for the whole inference
        # — serialising every session on the replica and stalling Serve health
        # checks. Offloading to a thread pool (sized to num_workers) keeps the
        # loop free to accept other sessions' requests while inference runs.
        self._executor = ThreadPoolExecutor(
            max_workers=num_workers, thread_name_prefix="stt-transcribe"
        )

    async def transcribe(self, audio: np.ndarray, language: str = "en") -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._transcribe_sync, audio, language
        )

    def _transcribe_sync(self, audio: np.ndarray, language: str) -> str:
        segments, _ = self.model_faster.transcribe(
            audio,
            language=language,
            beam_size=1,  # faster for realtime
        )
        return " ".join([seg.text for seg in segments]).strip()


class STT(ModuleWithHandle):
    """STT Module

    Transcribe voice using Faster_Whisper.

    Holds the per-session sliding-window buffer and delegates the actual
    transcription to a handle-backed STTHandle, so the Whisper model runs
    off the HuRI master node.

    input: voice,
    output: transcript

    :language: language spoken in the audio. It should be a language code such
        as "en" or "fr".
    :sample_rate: size of received voice audio. Usually 8000, 16000 or 48000.
    :block_duration: size of received voice audio (in s).
    :transcribe_window: duration of audio per transcription (in s).
    :transcribe_step: overlap between consecutive transcription windows (in s).
    """

    _handle_cls = STTHandle
    input_type = "voice"
    output_type = "transcript"

    def __init__(
        self,
        _handle: handle.DeploymentHandle,
        language: str = "en",
        sample_rate: int = 16000,
        block_duration: float = 0.020,  # s
        transcribe_window: float = 2.0,  # s
        transcribe_step: float = 1.0,  # s
        **kwargs,
    ):
        super().__init__(_handle=_handle, **kwargs)

        self.language = language

        self.sample_rate = sample_rate
        self.window_size: int = int(transcribe_window / block_duration)
        self.step_size: int = int(transcribe_step / block_duration)

        self.buffer: List[np.ndarray] = []
        # Number of leading frames in `buffer` already covered by a previous
        # transcription (the overlap kept when the window slides). Frames past
        # it are audio Whisper has not heard yet.
        self._covered: int = 0

        self.running = False
        self.lock: asyncio.Lock = asyncio.Lock()

        # Set when the end of the voice is received. Remembered if a
        # transcription is in flight, so that call handles it once it finishes.
        self._end_requested: bool = False

    async def process(self, voice: Voice) -> AsyncGenerator[Transcript, None]:
        async with self.lock:
            if voice.data is None:
                self._end_requested = True
            else:
                self.buffer.append(voice.data)
            if self.running:
                return
            self.running = True

        try:
            async for transcript in self._transcribe_pending():
                yield transcript
        finally:
            self.running = False

    async def _transcribe_pending(self) -> AsyncGenerator[Transcript, None]:
        """Run transcriptions until there is nothing left to do.

        Loops so that an end-of-turn that arrives while a window is being
        transcribed is handled right after it, in this same call. Each pass is
        yielded as its own Transcript.
        """

        while True:
            async with self.lock:
                end = self._end_requested
                if end:
                    self._end_requested = False
                    chunks = list(self.buffer)
                    if len(chunks) <= self._covered:
                        # Nothing new since the last window (only the already
                        # transcribed overlap is left): re-running Whisper on it
                        # would only repeat itself or invite hallucinations.
                        self.buffer = []
                        self._covered = 0
                        chunks = []
                else:
                    if len(self.buffer) < self.window_size:
                        return
                    chunks = self.buffer[: self.window_size]

            if end and not chunks:
                yield Transcript("", True)
                return

            audio = np.concatenate(chunks, axis=0)
            text = await self._handle.transcribe.remote(audio, self.language)

            async with self.lock:
                if end:
                    # Whole remaining buffer was transcribed: reset per-turn
                    # state so the NEXT utterance is independent. Frames that
                    # arrived during the await belong to the next turn.
                    self.buffer = self.buffer[len(chunks) :]
                    self._covered = 0
                else:
                    # Mid-utterance: slide the window forward, keeping the
                    # overlap.
                    processed = self.window_size - self.step_size
                    self.buffer = self.buffer[processed:]
                    self._covered = max(0, len(chunks) - processed)

            yield Transcript(text, end)
            if not self._end_requested:
                return
