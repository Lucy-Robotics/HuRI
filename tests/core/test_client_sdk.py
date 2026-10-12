"""The client SDK against a local websocket server standing in for HuRI."""

import asyncio
import json
from dataclasses import dataclass

import pytest
import websockets

from huri_core.core.client import HuRIClient, HuRISessionError, decode, encode
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.events import BytesEvent, JsonEvent


@dataclass
class Text(JsonEvent):
    text: str


class FakeHuRI:
    """Accepts or rejects the handshake, records frames, sends scripted ones."""

    def __init__(self, reply=None, outgoing=()):
        self.reply = reply or {"type": "session_init", "user_id": "u-server"}
        self.outgoing = list(outgoing)
        self.handshake = None
        self.received = []

    async def handler(self, ws):
        self.handshake = json.loads(await ws.recv())
        await ws.send(json.dumps(self.reply))
        if self.reply["type"] != "session_init":
            await ws.close()
            return
        for frame in self.outgoing:
            await ws.send(frame)
        async for message in ws:
            self.received.append(decode(message))

    async def __aenter__(self):
        self._server = await websockets.serve(self.handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_):
        self._server.close()
        await self._server.wait_closed()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


async def until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)


def test_frames_round_trip():
    assert decode(encode("huri.token", Text("hi"))) == ("huri.token", {"text": "hi"})
    assert decode(encode("huri.audio.in", BytesEvent(b"\x01\x02"))) == (
        "huri.audio.in",
        b"\x01\x02",
    )


def test_handshake_declares_the_session_and_server_parses_it():
    async def scenario():
        async with FakeHuRI() as server:
            client = HuRIClient(
                server.url,
                modules={"rag": {"name": "rag", "args": {"tone": "formal"}}},
                sends=["huri.question"],
                user_id="u-1",
            )
            client.on("huri.token", Text, lambda e: asyncio.sleep(0))
            async with client:
                assert client.user_id == "u-server"
            return server.handshake

    handshake = run(scenario())
    assert handshake["subscribe"] == ["huri.token"]
    assert handshake["send"] == ["huri.question"]
    config = ClientConfig.from_dict(handshake)
    assert config.user_id == "u-1" and config.modules["rag"].key == "rag"


def test_rejected_session_raises_with_the_server_modules():
    reply = {"type": "session_error", "error": "nope", "server_modules": ["stt"]}

    async def scenario():
        async with FakeHuRI(reply=reply) as server:
            with pytest.raises(HuRISessionError) as exc:
                await HuRIClient(server.url, modules={}).connect()
            return exc.value

    error = run(scenario())
    assert error.error == "nope" and error.server_modules == ["stt"]


def test_events_are_sent_as_json_or_binary():
    async def scenario():
        async with FakeHuRI() as server:
            client = HuRIClient(
                server.url, modules={}, sends=["huri.question", "huri.audio.in"]
            )
            async with client:
                await client.send("huri.question", Text("hello"))
                await client.send("huri.audio.in", BytesEvent(b"\x00\x01"))
                await until(lambda: len(server.received) == 2)
            return server.received

    assert run(scenario()) == [
        ("huri.question", {"text": "hello"}),
        ("huri.audio.in", b"\x00\x01"),
    ]


def test_sending_an_undeclared_topic_is_refused():
    async def scenario():
        async with FakeHuRI() as server:
            async with HuRIClient(server.url, modules={}, sends=[]) as client:
                with pytest.raises(ValueError, match="sends"):
                    await client.send("huri.question", Text("x"))

    run(scenario())


def test_handlers_of_a_topic_run_in_order_and_survive_failures():
    frames = [encode("huri.token", Text(str(i))) for i in range(5)]

    async def scenario():
        seen = []

        async def slow_then_fast(event: Text):
            # Earlier events sleep longer: concurrent handlers would reorder.
            await asyncio.sleep(0.05 - 0.01 * int(event.text))
            if event.text == "2":
                raise RuntimeError("handler bug")
            seen.append(event.text)

        async with FakeHuRI(outgoing=frames) as server:
            client = HuRIClient(server.url, modules={})
            client.on("huri.token", Text, slow_then_fast)
            async with client:
                await until(lambda: len(seen) == 4)
        return seen

    assert run(scenario()) == ["0", "1", "3", "4"]


def test_subscribing_after_connecting_is_refused():
    async def scenario():
        async with FakeHuRI() as server:
            async with HuRIClient(server.url, modules={}) as client:
                with pytest.raises(RuntimeError, match="before connecting"):
                    client.on("huri.token", Text, lambda e: asyncio.sleep(0))

    run(scenario())


def test_one_topic_cannot_be_decoded_as_two_classes():
    client = HuRIClient("ws://unused", modules={})
    client.on("huri.token", Text, lambda e: asyncio.sleep(0))
    with pytest.raises(ValueError, match="already subscribed"):
        client.on("huri.token", BytesEvent, lambda e: asyncio.sleep(0))


def test_old_client_config_format_gets_a_migration_hint():
    legacy = {"user_id": "u", "interface_path": "x:y", "senders": {}, "hooks": {}}
    with pytest.raises(ValueError, match="subscribe.*send"):
        ClientConfig.from_dict(legacy)
