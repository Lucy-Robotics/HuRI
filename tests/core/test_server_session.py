"""A whole session through the server's websocket endpoint, with toy modules
and events: the handshake, the routing of JSON and binary frames, and the
rejections. No Ray cluster: the ingress class runs in-process on a fake socket.
"""

import asyncio
import json
import struct
from dataclasses import dataclass
from typing import Any, Dict

import pytest

from huri_core.core.events import BytesEvent, JsonEvent
from huri_core.core.huri import HuRI
from huri_core.core.module import Module

# @serve.ingress subclasses the class it decorates (with an async __init__ for
# Serve); its direct base is HuRI as written, which runs without Serve.
Server = HuRI.func_or_class.__mro__[1]
assert "run_session" in vars(Server)


@dataclass
class Word(JsonEvent):
    text: str


class Chunk(BytesEvent):
    pass


class Upper(Module):
    input_type, output_type = "toy.word", "toy.loud"

    async def process(self, data: Word):
        return Word(data.text.upper())


class Reverse(Module):
    input_type, output_type = "toy.chunk", "toy.reversed"

    async def process(self, data: Chunk):
        return Chunk(data.data[::-1])


EVENTS = {
    "toy.word": Word,
    "toy.loud": Word,
    "toy.chunk": Chunk,
    "toy.reversed": Chunk,
}


def frame(topic: str, payload: bytes) -> bytes:
    return struct.pack("!H", len(topic)) + topic.encode() + payload


class FakeSocket:
    """The FastAPI WebSocket methods the endpoint uses. Replays `incoming`
    (handshake first), then disconnects."""

    def __init__(self, handshake, *incoming):
        self.handshake = handshake
        self.incoming = list(incoming)
        self.sent = []
        self.closed = None

    async def accept(self):
        pass

    async def receive_json(self):
        if isinstance(self.handshake, Exception):
            raise self.handshake
        return self.handshake

    async def receive(self):
        await asyncio.sleep(0.02)  # let the previous event go through the bus
        if not self.incoming:
            return {"type": "websocket.disconnect"}
        message = self.incoming.pop(0)
        key = "bytes" if isinstance(message, bytes) else "text"
        return {"type": "websocket.receive", key: message}

    async def send_json(self, message):
        self.sent.append(message)

    async def send_bytes(self, payload):
        self.sent.append(payload)

    async def close(self, code=None, reason=None):
        self.closed = code


def session(ws: FakeSocket, modules=None) -> FakeSocket:
    server = Server(
        modules or {"upper": Upper, "reverse": Reverse}, {}, dict(EVENTS), ()
    )
    return session_on(server, ws)


def session_on(server, ws: FakeSocket) -> FakeSocket:
    asyncio.run(asyncio.wait_for(server.run_session(ws), timeout=10))
    return ws


def handshake(modules, subscribe, send):
    return {"user_id": "u1", "modules": modules, "subscribe": subscribe, "send": send}


def test_json_and_binary_frames_go_through_the_pipeline_and_back():
    ws = session(
        FakeSocket(
            handshake(
                {"a": {"name": "upper"}, "b": {"name": "reverse"}},
                subscribe=["toy.loud", "toy.reversed"],
                send=["toy.word", "toy.chunk"],
            ),
            json.dumps({"topic": "toy.word", "data": {"text": "hi"}}),
            frame("toy.chunk", b"\x01\x02\x03"),
        )
    )
    assert ws.sent == [
        {"type": "session_init", "user_id": "u1"},
        {"topic": "toy.loud", "data": {"text": "HI"}},
        frame("toy.reversed", b"\x03\x02\x01"),
    ]


def test_a_frame_on_an_unknown_topic_is_dropped_not_fatal():
    ws = session(
        FakeSocket(
            handshake({"a": {"name": "upper"}}, ["toy.loud"], ["toy.word"]),
            json.dumps({"topic": "toy.nope", "data": {}}),
            json.dumps({"topic": "toy.word", "data": {"text": "still here"}}),
        )
    )
    assert ws.sent[-1] == {"topic": "toy.loud", "data": {"text": "STILL HERE"}}


def test_an_unknown_module_is_rejected_with_the_servers_modules():
    ws = session(FakeSocket(handshake({"a": {"name": "stt"}}, [], ["toy.word"])))
    (reply,) = ws.sent
    assert reply["type"] == "session_error"
    assert "Unknown module 'stt'" in reply["error"]
    assert reply["server_modules"] == ["reverse", "upper"]
    assert ws.closed == 1008


def test_an_incomplete_session_is_rejected_before_it_starts():
    ws = session(FakeSocket(handshake({"a": {"name": "upper"}}, ["toy.loud"], [])))
    (reply,) = ws.sent
    assert reply["type"] == "session_error"
    assert "Upper consumes 'toy.word'" in reply["error"]


def test_a_malformed_handshake_gets_a_session_error():
    """Not JSON, or not an object: still a session_error, never a dropped socket."""
    for handshake_ in (json.JSONDecodeError("bad", "x", 0), ["not", "an", "object"]):
        ws = session(FakeSocket(handshake_))
        assert ws.sent[0]["type"] == "session_error" and ws.closed == 1008


class Servo(Module):
    input_type, output_type = "toy.word", None
    actuation = True

    async def process(self, data: Word):
        return None


def get(server, path: str):
    """GET `path` on a FastAPI app routing to `server`'s endpoints, through ASGI:
    status and decoded JSON body."""

    from fastapi import FastAPI

    api = FastAPI()
    api.add_api_route("/modules", server.list_modules, methods=["GET"])
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(api(scope, receive, send))
    status = messages[0]["status"]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return status, json.loads(body)


def app_routes(router):
    """Every route of `router`, including those of included routers."""

    for route in router.routes:
        inner = getattr(route, "original_router", None)  # an included router
        if inner is not None:
            yield from app_routes(inner)
        else:
            yield route


def test_the_ingress_declares_get_modules():
    from huri_core.core.app import app

    assert ("/modules", ("GET",)) in {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ())))
        for route in app_routes(app)
    }


def test_the_ingress_route_serves_list_modules_unchanged():
    """The tests above mount `list_modules` on their own app: check that the
    real route is that endpoint, with nothing reshaping or guarding its answer
    (Serve binds the deployment instance to it at runtime)."""

    from huri_core.core.app import app

    (route,) = [r for r in app_routes(app) if r.path == "/modules"]
    assert route.endpoint.__name__ == "list_modules"
    assert route.response_model in (None, Dict[str, Any])
    assert not route.dependencies and not route.dependant.dependencies


def test_get_modules_lists_the_enabled_modules_their_topics_and_actuation():
    import huri_core

    server = Server(
        {"upper": Upper, "reverse": Reverse, "servo": Servo},
        {},
        dict(EVENTS),
        actuating=["servo"],
    )
    status, body = get(server, "/modules")
    assert status == 200
    assert body == {
        "modules": ["reverse", "servo", "upper"],
        "details": {
            "reverse": {
                "input": "toy.chunk",
                "output": "toy.reversed",
                "actuation": False,
            },
            "servo": {"input": "toy.word", "output": None, "actuation": True},
            "upper": {"input": "toy.word", "output": "toy.loud", "actuation": False},
        },
        "huri_core": huri_core.__version__,
    }
    # The same names this server tells a rejected session about.
    ws = session_on(
        server, FakeSocket(handshake({"a": {"name": "stt"}}, [], ["toy.word"]))
    )
    assert ws.sent[0]["type"] == "session_error"
    assert ws.sent[0]["server_modules"] == body["modules"]


def test_actuating_is_a_required_argument():
    """A binder that forgets it must fail, not report a servo as safe."""

    with pytest.raises(TypeError):
        Server({"servo": Servo}, {}, dict(EVENTS))


def test_get_modules_leaves_out_installed_but_disabled_modules(monkeypatch):
    import huri_core.core.huri as huri_module

    monkeypatch.setattr(huri_module, "installed_modules", lambda: ["upper", "tts"])
    server = Server({"upper": Upper}, {}, dict(EVENTS), ())
    _, body = get(server, "/modules")
    assert body["modules"] == ["upper"]
    assert list(body["details"]) == ["upper"]


def test_get_modules_imports_nothing(monkeypatch):
    """Answered from what the server registered at startup: no entry point is
    loaded, no module instantiated."""

    import huri_core.core.huri as huri_module
    import huri_core.core.plugins as plugins

    server = Server({"upper": Upper}, {}, dict(EVENTS), ())

    def forbidden(*args, **kwargs):
        raise AssertionError("GET /modules must not load or build a module")

    for target, name in [
        (plugins, "load_modules"),
        (plugins, "load_events"),
        (plugins, "installed_modules"),
        (huri_module, "installed_modules"),
    ]:
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(server.module_factory, "create", forbidden)
    monkeypatch.setattr(Upper, "__init__", forbidden)
    status, body = get(server, "/modules")
    assert status == 200 and body["modules"] == ["upper"]
