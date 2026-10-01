import asyncio
from dataclasses import dataclass

import numpy as np
import pytest

from src.core.bus import EventGraph
from src.core.events import EventData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyModule:
    input_type = None
    output_type = None

    async def process(self, data):
        raise NotImplementedError


@dataclass
class DummyEvent(EventData):
    value: int


@dataclass
class ArrayEvent(EventData):
    data: np.ndarray


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


def test_register():
    graph = EventGraph()

    module = DummyModule()
    module.input_type = "input"

    graph.register(module)

    assert module in graph.subscribers["input"]


# ---------------------------------------------------------------------------
# publish()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_calls_subscriber(monkeypatch):
    graph = EventGraph()

    called = asyncio.Event()

    class Module(DummyModule):
        input_type = "input"

        async def process(self, data):
            called.set()
            return None

    module = Module()
    graph.register(module)

    await graph.publish("input", DummyEvent(1))

    await asyncio.wait_for(called.wait(), timeout=1)


# ---------------------------------------------------------------------------
# coroutine output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coroutine_output_published():
    graph = EventGraph()

    received = asyncio.Event()

    class Producer(DummyModule):
        input_type = "start"
        output_type = "next"

        async def process(self, data):
            return DummyEvent(42)

    class Consumer(DummyModule):
        input_type = "next"

        async def process(self, data):
            assert data.value == 42
            received.set()

    graph.register(Producer())
    graph.register(Consumer())

    await graph.publish("start", DummyEvent(0))

    await asyncio.wait_for(received.wait(), timeout=1)


# ---------------------------------------------------------------------------
# async generator output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_generator_output():
    graph = EventGraph()

    values = []

    class Producer(DummyModule):
        input_type = "start"
        output_type = "stream"

        async def process(self, data):
            yield DummyEvent(1)
            yield DummyEvent(2)
            yield DummyEvent(3)

    class Consumer(DummyModule):
        input_type = "stream"

        async def process(self, data):
            values.append(data.value)

    graph.register(Producer())
    graph.register(Consumer())

    await graph.publish("start", DummyEvent(0))

    await asyncio.sleep(0.1)

    assert values == [1, 2, 3]


# ---------------------------------------------------------------------------
# None outputs ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_output_not_published():
    graph = EventGraph()

    called = False

    class Producer(DummyModule):
        input_type = "start"
        output_type = "next"

        async def process(self, data):
            return None

    class Consumer(DummyModule):
        input_type = "next"

        async def process(self, data):
            nonlocal called
            called = True

    graph.register(Producer())
    graph.register(Consumer())

    await graph.publish("start", DummyEvent(0))
    await asyncio.sleep(0.05)

    assert not called


# ---------------------------------------------------------------------------
# recursive routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recursive_routing():
    graph = EventGraph()

    done = asyncio.Event()

    class First(DummyModule):
        input_type = "a"
        output_type = "b"

        async def process(self, data):
            return DummyEvent(data.value + 1)

    class Second(DummyModule):
        input_type = "b"

        async def process(self, data):
            assert data.value == 2
            done.set()

    graph.register(First())
    graph.register(Second())

    await graph.publish("a", DummyEvent(1))

    await asyncio.wait_for(done.wait(), timeout=1)
