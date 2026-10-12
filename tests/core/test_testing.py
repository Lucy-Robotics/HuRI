"""The module test harness (huri_core.testing) itself."""

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from huri_core.core.events import JsonEvent
from huri_core.core.module import Module, ModuleWithHandle, ModuleWithId
from huri_core.testing import Call, LocalHandle, ModuleHarness

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Word(JsonEvent):
    text: str


@dataclass
class Count(JsonEvent):
    n: int


class Counter(Module):
    """Emits the running word count every 2 words."""

    input_type, output_type = "toy.word", "toy.count"
    input_event, output_event = Word, Count

    def __init__(self, every: int = 2):
        self.every, self.seen = every, 0

    async def process(self, data: Word):
        self.seen += 1
        return Count(self.seen) if self.seen % self.every == 0 else None


class Splitter(Module):
    input_type, output_type = "toy.word", "toy.word"
    input_event, output_event = Word, Word

    async def process(self, data: Word):
        for part in data.text.split():
            yield Word(part)
        yield None  # skipped, as the bus does


class Liar(Module):
    input_type, output_type = "toy.word", "toy.count"
    output_event = Count

    async def process(self, data):
        return Word("not a count")


class Shouter(ModuleWithHandle, ModuleWithId):
    """Calls its deployment both ways a real module does."""

    input_type, output_type = "toy.word", "toy.word"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.finalized = False

    async def process(self, data: Word):
        loud = await self._handle.upper.remote(data.text)
        async for letter in self._handle.options(stream=True).letters.remote(loud):
            yield Word(f"{self._user_id}:{letter}")

    async def finalize(self):
        await self._handle.bye.remote()
        self.finalized = True


class FakeDeployment:
    def upper(self, text):  # sync methods work too
        return text.upper()

    async def letters(self, text):  # async generator, streamed
        for c in text:
            yield c

    async def bye(self):
        return None


def test_coroutine_module_emits_its_returned_events():
    assert ModuleHarness(Counter).run(*[Word(w) for w in "a b c d e".split()]) == [
        Count(2),
        Count(4),
    ]


def test_module_args_and_instances_are_accepted():
    assert ModuleHarness(Counter, every=1).run(Word("a")) == [Count(1)]
    assert ModuleHarness(Counter(every=3)).run(Word("a"), Word("b"), Word("c")) == [
        Count(3)
    ]


def test_generator_module_emits_every_item_but_none():
    assert ModuleHarness(Splitter).run(Word("hi there")) == [Word("hi"), Word("there")]


def test_wrong_input_event_is_reported():
    with pytest.raises(AssertionError, match="input got Count.*input_event = Word"):
        ModuleHarness(Counter).run(Count(1))


def test_wrong_output_event_is_reported():
    with pytest.raises(
        AssertionError, match="output emitted Word.*output_event = Count"
    ):
        ModuleHarness(Liar).run(Word("x"))


def test_module_with_handle_requires_one():
    with pytest.raises(TypeError, match="LocalHandle"):
        ModuleHarness(Shouter)


def test_local_handle_supports_awaited_and_streamed_calls():
    handle = LocalHandle(FakeDeployment())
    harness = ModuleHarness(Shouter, handle=handle, user_id="u1")
    assert harness.run(Word("ok")) == [Word("u1:O"), Word("u1:K")]
    assert handle.calls == [Call("upper", ("ok",)), Call("letters", ("OK",))]

    asyncio.run(harness.finalize())
    assert harness.module.finalized and handle.calls[-1] == Call("bye", ())


def test_local_handle_reports_a_method_the_fake_lacks():
    with pytest.raises(AttributeError, match="FakeDeployment has no method 'nope'"):
        LocalHandle(FakeDeployment()).nope


def test_a_real_streaming_module_runs_on_a_fake_deployment():
    from huri_events.question import RAGQuestion
    from huri_events.speech import Transcript
    from huri_events.text import Token

    from huri_core.modules.rag.rag import RAG

    class FakeRAGHandle:
        async def stream(self, query):
            for delta in ("Hello", " ", query.question):
                yield delta

        async def save_conversation(self, user_id, history):
            self.saved = (user_id, history)

    fake = FakeRAGHandle()
    harness = ModuleHarness(RAG, handle=LocalHandle(fake), user_id="alice")
    out = harness.run(RAGQuestion(Transcript("world", True), None))
    assert out == [
        Token("Hello", False),
        Token(" ", False),
        Token("world", False),
        Token("", True),
    ]

    asyncio.run(harness.finalize())
    assert fake.saved[0] == "alice" and fake.saved[1][-1]["content"] == "Hello world"


def test_harness_needs_no_ray_server_or_client_stack():
    """Usable with only the base huri-core install."""
    code = (
        "import sys\n"
        "for m in ('ray', 'fastapi', 'websockets', 'omegaconf'):\n"
        "    sys.modules[m] = None\n"
        "from tests.core.test_testing import Counter, Word\n"
        "from huri_core.testing import ModuleHarness\n"
        "assert len(ModuleHarness(Counter).run(Word('a'), Word('b'))) == 1\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


class WaitsForTheEnd(Module):
    """Like TTS: the first event's output only comes once the last arrived."""

    input_type, output_type = "toy.word", "toy.word"

    def __init__(self):
        self.done = asyncio.Event()
        self.words = []

    async def process(self, data: Word):
        self.words.append(data.text)
        if data.text == "end":
            self.done.set()
            return
        if len(self.words) == 1:
            await self.done.wait()
            yield Word(" ".join(self.words))


def test_feed_all_runs_events_concurrently_like_the_bus():
    out = asyncio.run(
        asyncio.wait_for(
            ModuleHarness(WaitsForTheEnd).feed_all(Word("a"), Word("b"), Word("end")),
            timeout=5,
        )
    )
    assert out == [Word("a b end")]
