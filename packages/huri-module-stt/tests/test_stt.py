import asyncio

import numpy as np
from huri_events.speech import Transcript, Voice

from huri_core.testing import LocalHandle, ModuleHarness


class FakeWhisper:
    """Stands in for STTHandle: one scripted text per call; can block the first."""

    def __init__(self, texts, gate=None):
        self.texts = list(texts)
        self.gate = gate
        self.calls = []

    async def transcribe(self, audio, language):
        self.calls.append(len(audio))
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()
        return self.texts.pop(0)


def _stt(whisper):
    from huri_stt.module import STT

    # 20 ms blocks -> window 10 frames, step 5 frames (fast tests).
    return ModuleHarness(
        STT, handle=LocalHandle(whisper), transcribe_window=0.2, transcribe_step=0.1
    )


def _voice():
    return Voice(np.zeros(4, dtype=np.float32))


async def _feed(stt, n):
    return await stt.feed_all(*[_voice() for _ in range(n)])


def test_end_event_during_inflight_transcription_is_not_lost():
    async def run():
        gate = asyncio.Event()
        h = FakeWhisper(["what is the", "the weather"], gate)
        stt = _stt(h)
        first = asyncio.create_task(_feed(stt, 10))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # In-flight call is blocked: more speech arrives, then the VAD
        # reports end of turn.
        for _ in range(12):
            assert await stt.feed(_voice()) == []
        assert await stt.feed(Voice(None)) == []
        gate.set()
        out = await first
        # STT does not merge: one Transcript per pass, in order (TAG stitches
        # them, see huri-module-tag's tests).
        assert out == [
            Transcript("what is the", False),
            Transcript("the weather", True),
        ]

    asyncio.run(run())


def test_tail_beyond_first_window_is_transcribed_at_end():
    async def run():
        h = FakeWhisper(["b c d e f"])
        stt = _stt(h)
        # Backlog: more frames buffered than one window, then the turn ends.
        stt.module.buffer = [np.zeros(4, np.float32)] * 14
        out = await stt.feed(Voice(None))
        assert out == [Transcript("b c d e f", True)]
        assert h.calls == [14 * 4]  # whole buffer, not just first window
        assert stt.module.buffer == [] and stt.module._covered == 0

    asyncio.run(run())


def test_end_with_no_new_audio_skips_whisper():
    async def run():
        h = FakeWhisper(["hello world", "should not be called"])
        stt = _stt(h)
        out = await _feed(stt, 10)
        assert out == [Transcript("hello world", False)]
        stt.module.buffer = stt.module.buffer[
            : stt.module._covered
        ]  # only overlap left
        out = await stt.feed(Voice(None))
        assert out == [Transcript("", True)]
        assert len(h.calls) == 1

    asyncio.run(run())


def test_short_utterance_below_one_window_is_still_transcribed():
    async def run():
        h = FakeWhisper(["yes"])
        stt = _stt(h)
        out = await _feed(stt, 3)  # far less than one window / overlap
        assert out == []
        out = await stt.feed(Voice(None))
        assert out == [Transcript("yes", True)]

    asyncio.run(run())
