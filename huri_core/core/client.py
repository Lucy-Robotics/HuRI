"""Client SDK: a small transport object for talking to a HuRI server.

    client = HuRIClient(url, modules={"rag": {"name": "rag"}}, sends=["huri.question"])
    client.on("huri.token", Token, handle_token)
    async with client:
        await client.send("huri.question", RAGQuestion(...))
        await client.wait_closed()

The SDK owns the websocket, the session handshake and the wire encoding (JSON
frames, and binary frames `[u16 topic length][topic][payload]`). It knows
nothing about audio, text or gestures: the use case registers the handlers.

Handlers of one topic run one at a time, in arrival order (audio playback
needs that); different topics run concurrently. A failing handler is logged
and does not stop the others.
"""

import asyncio
import json
import logging
import struct
import uuid
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Type,
    TypeVar,
)

import websockets
from omegaconf import OmegaConf

from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.events import EventData
from huri_core.core.user_config import get_or_create_and_save_user_id

logger = logging.getLogger("huri.client")

E = TypeVar("E", bound=EventData)
Handler = Callable[[Any], Awaitable[None]]


class HuRISessionError(RuntimeError):
    """The server refused the session (unknown module, broken wiring...)."""

    def __init__(self, error: str, server_modules: Optional[List[str]] = None):
        self.error = error
        self.server_modules = server_modules or []
        super().__init__(error)


@dataclass
class _Subscription:
    event_cls: Type[EventData]
    handlers: List[Handler]
    queue: "asyncio.Queue[Any]"


def load_raw_client_config(path: str) -> Dict[str, Any]:
    """A client YAML, interpolations resolved, with this machine's user_id
    (persisted under `user_id_file_path`, or the user config dir) added."""

    with open(path) as f:
        resolved = OmegaConf.to_container(OmegaConf.load(f), resolve=True)
    if not isinstance(resolved, dict):
        raise ValueError(f"{path}: a client config must be a YAML mapping")
    raw: Dict[str, Any] = {str(k): v for k, v in resolved.items()}
    raw["user_id"] = get_or_create_and_save_user_id(raw.get("user_id_file_path"))
    return raw


def load_client_config(path: str) -> ClientConfig:
    return ClientConfig.from_dict(load_raw_client_config(path))


def encode(topic: str, event: EventData) -> str | bytes:
    """Wire frame of an event: binary when to_wire() gives bytes, else JSON."""

    wire = event.to_wire()
    if isinstance(wire, bytes):
        topic_bytes = topic.encode()
        return struct.pack(">H", len(topic_bytes)) + topic_bytes + wire
    return json.dumps({"topic": topic, "data": wire})


def decode(message: str | bytes) -> tuple[str, Any]:
    """(topic, raw wire data) of a frame."""

    if isinstance(message, bytes):
        topic_len = struct.unpack(">H", message[:2])[0]
        return message[2 : 2 + topic_len].decode(), message[2 + topic_len :]
    frame = json.loads(message)
    return frame["topic"], frame["data"]


class HuRIClient:
    """Connection to a HuRI server.

    :url: websocket URL of the server session endpoint.
    :modules: the session's pipeline, as in the YAML `modules:` section
        ({id: {"name": "stt", "args": {...}}} or {"module": "pkg.mod:Cls"}).
    :sends: topics this client will send; the server checks every module input
        has a producer, and send() refuses any other topic.
    :user_id: identity the server keeps per-user state under (RAG memory).
        A random one is used when omitted.
    """

    def __init__(
        self,
        url: str,
        modules: Mapping[str, Mapping[str, Any]],
        sends: Iterable[str] = (),
        user_id: Optional[str] = None,
    ):
        self.url = url
        self.modules = {k: dict(v) for k, v in modules.items()}
        self.sends = list(sends)
        self.user_id = user_id or str(uuid.uuid4())
        self._subs: Dict[str, _Subscription] = {}
        self._ws: Optional[websockets.ClientConnection] = None
        self._tasks: List[asyncio.Task] = []

    @classmethod
    def from_config(cls, config: ClientConfig) -> "HuRIClient":
        """A client for a session described by a client YAML (load_client_config).
        Its `subscribe` topics still need handlers, registered with on()."""

        return cls(
            config.huri_url,
            modules={k: m.to_dict() for k, m in config.modules.items()},
            sends=config.send,
            user_id=config.user_id,
        )

    # --- configuration ----------------------------------------------------

    def on(
        self, topic: str, event_cls: Type[E], handler: Callable[[E], Awaitable[None]]
    ) -> None:
        """Call `handler(event)` for every `topic` event. Before connecting:
        the subscriptions are part of the session handshake."""

        if self._ws is not None:
            raise RuntimeError("subscribe with on() before connecting")
        sub = self._subs.get(topic)
        if sub is None:
            self._subs[topic] = _Subscription(event_cls, [handler], asyncio.Queue())
        elif sub.event_cls is not event_cls:
            raise ValueError(
                f"{topic!r} is already subscribed as {sub.event_cls.__name__}, "
                f"not {event_cls.__name__}"
            )
        else:
            sub.handlers.append(handler)

    def handshake(self) -> Dict[str, Any]:
        """The session request sent to the server."""

        return {
            "user_id": self.user_id,
            "modules": self.modules,
            "subscribe": sorted(self._subs),
            "send": self.sends,
        }

    # --- connection ---------------------------------------------------------

    async def connect(self) -> "HuRIClient":
        ws = await websockets.connect(self.url)
        try:
            await ws.send(json.dumps(self.handshake()))
            try:
                reply = json.loads(await ws.recv())
            except websockets.ConnectionClosed as e:
                raise HuRISessionError(
                    "the server closed the connection without a session reply"
                ) from e
            if reply.get("type") == "session_error":
                raise HuRISessionError(
                    reply.get("error", ""), reply.get("server_modules")
                )
            if reply.get("type") != "session_init":
                raise HuRISessionError(f"unexpected first message: {reply!r}")
        except BaseException:
            await ws.close()
            raise

        self.user_id = reply.get("user_id", self.user_id)
        self._ws = ws
        self._tasks = [asyncio.create_task(self._receive_loop(ws))]
        self._tasks += [
            asyncio.create_task(self._dispatch_loop(topic, sub))
            for topic, sub in self._subs.items()
        ]
        return self

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> "HuRIClient":
        return await self.connect()

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def wait_closed(self) -> None:
        """Until the server closes the session."""

        if self._ws is not None:
            await self._ws.wait_closed()

    # --- traffic -----------------------------------------------------------

    async def send(self, topic: str, event: EventData) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        if topic not in self.sends:
            raise ValueError(
                f"{topic!r} is not in this client's sends {self.sends}: declare it "
                "so the server can check the session wiring"
            )
        await self._ws.send(encode(topic, event))

    async def _receive_loop(self, ws: websockets.ClientConnection) -> None:
        try:
            async for message in ws:
                topic, data = decode(message)
                sub = self._subs.get(topic)
                if sub is None:
                    logger.warning("dropping frame on unsubscribed topic %r", topic)
                    continue
                sub.queue.put_nowait(data)
        except websockets.ConnectionClosed:
            pass

    async def _dispatch_loop(self, topic: str, sub: _Subscription) -> None:
        while True:
            data = await sub.queue.get()
            try:
                # from_wire is the decoding contract for both wire shapes.
                event = sub.event_cls.from_wire(data)
            except Exception:
                logger.exception("cannot decode %r frame as %s", topic, sub.event_cls)
                continue
            for handler in sub.handlers:
                try:
                    await handler(event)
                except Exception:
                    logger.exception("handler for %r failed", topic)
