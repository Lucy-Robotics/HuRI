"""StreamingTTS on a fake engine, fed the way the bus feeds it: one concurrent
process() call per token."""

import asyncio

import numpy as np
from huri_events.audio import Audio
from huri_events.text import Token
from huri_tts import StreamingTTS

from huri_core.testing import LocalHandle, ModuleHarness


class FakeEngine:
    """The four methods StreamingTTS drives; 10 samples per pushed character."""

    def __init__(self):
        self.sessions = {}

    async def get_sample_rate(self):
        return 16000

    async def start_session(self, sid):
        self.sessions[sid] = {"texts": [], "done": asyncio.Event()}

    async def push_text(self, sid, text, end):
        await asyncio.sleep(0)  # let concurrent pushes interleave if they could
        session = self.sessions[sid]
        session["texts"].append(text)
        if end:
            session["done"].set()

    async def stream_audio(self, sid):
        session = self.sessions[sid]
        await session["done"].wait()
        for text in session["texts"]:
            if text:
                yield Audio(np.ones(len(text) * 10, dtype=np.float32), 16000)


class Engine(StreamingTTS):
    _handle_cls = FakeEngine


async def speak(harness, tokens):
    return await asyncio.gather(*(harness.feed(t) for t in tokens))


def test_an_utterance_streams_in_token_order_and_ends_with_a_marker():
    engine = FakeEngine()
    harness = ModuleHarness(Engine, handle=LocalHandle(engine))
    tokens = [Token("Hello", False), Token(" there", False), Token("", True)]
    first, *rest = asyncio.run(speak(harness, tokens))

    assert rest == [[], []]  # only the utterance's first call yields audio
    (session,) = engine.sessions.values()
    assert session["texts"] == ["Hello", " there", ""]
    assert [chunk.data.size for chunk in first] == [50, 60, 0]
    assert [chunk.pts for chunk in first[:2]] == [0.0, 50 / 16000]
    assert first[-1].end and first[-1].sample_rate == 16000


def test_each_utterance_gets_its_own_session():
    engine = FakeEngine()
    harness = ModuleHarness(Engine, handle=LocalHandle(engine))

    async def two_utterances():  # one event loop, like the server
        await speak(harness, [Token("One.", False), Token("", True)])
        await speak(harness, [Token("Two.", False), Token("", True)])

    asyncio.run(two_utterances())
    assert [s["texts"] for s in engine.sessions.values()] == [
        ["One.", ""],
        ["Two.", ""],
    ]
