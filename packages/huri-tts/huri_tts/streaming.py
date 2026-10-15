"""The engine-agnostic half of HuRI's text-to-speech modules."""

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator

import numpy as np
from huri_events.audio import Audio
from huri_events.text import Token

from huri_core.core.module import ModuleWithHandle

if TYPE_CHECKING:
    from ray.serve import handle

_END_AUDIO = object()  # sentinel pushed into the audio queue when synthesis ends


class StreamingTTS(ModuleWithHandle):
    """Streaming TTS module — bistream tokens-in / audio-out, engine-agnostic.

    Opens one synthesis session per utterance (delimited by `token.end`). Each
    incoming token is pushed straight into the model's text generator so audio
    starts coming back before the LLM has finished producing the response.
    No clause buffering on our side — CosyVoice's frontend handles segmentation
    and stitches LM calls together across the whole utterance.

    It drives any deployment exposing ``get_sample_rate()``,
    ``start_session(sid)``, ``push_text(sid, text, end)`` and
    ``stream_audio(sid)`` (an async generator of Audio); an engine package
    subclasses it and sets ``_handle_cls`` to its deployment.

    input: huri.token (Token)
    output: huri.audio.out (Audio)
    """

    input_type = "huri.token"
    output_type = "huri.audio.out"
    input_event = Token
    output_event = Audio

    def __init__(self, _handle: "handle.DeploymentHandle"):
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
