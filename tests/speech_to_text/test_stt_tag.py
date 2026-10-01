import asyncio

import numpy as np
import pytest

from src.modules.speech_to_text.events import Transcript, Voice
from src.modules.speech_to_text.text_aggregator import TAG


@pytest.mark.parametrize(
    "current,new,expected",
    [
        (
            "what is the weather",
            "the weather like in Paris today",
            "what is the weather like in Paris today",
        ),
        # No overlap must never corrupt/replace the sentence.
        (
            "what is the weather like",
            "in Paris today",
            "what is the weather like in Paris today",
        ),
        (
            "What is the weather like.",
            "weather like in Paris today?",
            "What is the weather like in Paris today?",
        ),
        (
            "I want to book a table for two",
            "for two people at eight",
            "I want to book a table for two people at eight",
        ),
        ("it is a very very", "very very long day", "it is a very very long day"),
        # Word cut by the window edge.
        ("what is the weath", "weather like", "what is the weather like"),
        # New text fully contained in the tail.
        ("hello there my friend", "my friend", "hello there my friend"),
        ("", "hello", "hello"),
        ("hello", "", "hello"),
    ],
)
def test_merge(current, new, expected):
    assert TAG()._merge(current, new) == expected


def test_tag_emits_on_end_and_resets():
    async def run():
        tag = TAG()
        assert await tag.process(Transcript("what is the", False)) is None
        assert await tag.process(Transcript("the weather", False)) is None
        q = await tag.process(Transcript("weather like", True))
        assert q.transcript.text == "what is the weather like"
        assert await tag.process(Transcript("", True)) is None
        q = await tag.process(Transcript("next question", True))
        assert q.transcript.text == "next question"

    asyncio.run(run())


class FakeHandle:
    """Returns one scripted text per call; can block the first call."""

    def __init__(self, texts, gate=None):
        self.texts = list(texts)
        self.gate = gate
        self.calls = []
        self.transcribe = self

    async def remote(self, audio, language):
        self.calls.append(len(audio))
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()
        return self.texts.pop(0)


def _stt(handle):
    from src.modules.speech_to_text.speech_to_text import STT

    # 20 ms blocks -> window 10 frames, step 5 frames (fast tests).
    return STT(_handle=handle, transcribe_window=0.2, transcribe_step=0.1)


async def _collect(stt, voice):
    return [t async for t in stt.process(voice)]


def _voice():
    return Voice(np.zeros(4, dtype=np.float32))


async def _feed(stt, n):
    out = []
    for _ in range(n):
        out += await _collect(stt, _voice())
    return out


def test_end_event_during_inflight_transcription_is_not_lost():
    async def run():
        gate = asyncio.Event()
        h = FakeHandle(["what is the", "the weather"], gate)
        stt = _stt(h)
        first = asyncio.create_task(_feed(stt, 10))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # In-flight call is blocked: more speech arrives, then the VAD
        # reports end of turn.
        for _ in range(12):
            assert await _collect(stt, _voice()) == []
        assert await _collect(stt, Voice(None)) == []
        gate.set()
        out = await first
        # STT does not merge: one Transcript per pass, in order.
        assert out == [
            Transcript("what is the", False),
            Transcript("the weather", True),
        ]
        # ...TAG stitches them.
        tag = TAG()
        question = None
        for t in out:
            question = await tag.process(t) or question
        assert question.transcript.text == "what is the weather"

    asyncio.run(run())


def test_tail_beyond_first_window_is_transcribed_at_end():
    async def run():
        h = FakeHandle(["b c d e f"])
        stt = _stt(h)
        # Backlog: more frames buffered than one window, then the turn ends.
        stt.buffer = [np.zeros(4, np.float32)] * 14
        out = await _collect(stt, Voice(None))
        assert out == [Transcript("b c d e f", True)]
        assert h.calls == [14 * 4]  # whole buffer, not just first window
        assert stt.buffer == [] and stt._covered == 0

    asyncio.run(run())


def test_end_with_no_new_audio_skips_whisper():
    async def run():
        h = FakeHandle(["hello world", "should not be called"])
        stt = _stt(h)
        out = await _feed(stt, 10)
        assert out == [Transcript("hello world", False)]
        stt.buffer = stt.buffer[: stt._covered]  # only overlap left
        out = await _collect(stt, Voice(None))
        assert out == [Transcript("", True)]
        assert len(h.calls) == 1

    asyncio.run(run())


def test_short_utterance_below_one_window_is_still_transcribed():
    async def run():
        h = FakeHandle(["yes"])
        stt = _stt(h)
        out = await _feed(stt, 3)  # far less than one window / overlap
        assert out == []
        out = await _collect(stt, Voice(None))
        assert out == [Transcript("yes", True)]

    asyncio.run(run())


def test_min_prefix_is_configurable():
    # "th" (from a cut "the") only matches when min_prefix allows 2 chars.
    assert TAG(min_prefix=3)._merge("what is th", "the weather") == (
        "what is th the weather"
    )
    assert TAG(min_prefix=2)._merge("what is th", "the weather") == (
        "what is the weather"
    )

    async def run():
        tag = TAG(min_prefix=2)
        await tag.process(Transcript("what is th", False))
        q = await tag.process(Transcript("the weather", True))
        assert q.transcript.text == "what is the weather"

    asyncio.run(run())
