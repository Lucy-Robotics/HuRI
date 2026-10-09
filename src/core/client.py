import asyncio
import importlib
import json
import struct
import traceback
from collections import defaultdict
from dataclasses import asdict
from typing import Any, Dict, Generic, List, Mapping, Type, TypeVar

import websockets

from src.core.dataclasses.config import ClientConfig
from src.core.events import EventData

T = TypeVar("T", bound=EventData)


class ClientSender(Generic[T]):
    """This class abstract sending data to HuRI.

    output_type: is the event data structure that the ClientSender will send.
    It can be EventData or bytes, and must match event topic it send.

    Class derived from ClientSender must implement input_loop,
    and use ClientSender.send to send data to HuRI.

    `singletton` is available to access shared ressources.
    """

    output_type: Type[T]

    def __init__(self, topic: str, singletton: Any, **_):
        """
        :topic: topic sent to HuRI
        :singletton: allow to get shared ressources"""
        self.topic = topic
        self.singletton = singletton

    async def input_loop(self, ws: websockets.ClientConnection):
        raise NotImplementedError

    async def _send_bytes(self, ws: websockets.ClientConnection, data: bytes):
        topic_bytes = self.topic.encode()
        packet = struct.pack(">H", len(topic_bytes)) + topic_bytes + data

        await ws.send(packet)

    async def _send_event_data(
        self, ws: websockets.ClientConnection, data: Mapping[str, Any]
    ):
        packet = json.dumps({"topic": self.topic, "data": data})

        await ws.send(packet)

    async def send(self, ws: websockets.ClientConnection, data: T):
        wire = data.to_wire()

        if isinstance(wire, bytes):
            await self._send_bytes(ws, wire)
        else:
            await self._send_event_data(ws, wire)


class ClientHook(Generic[T]):
    """This class abstract processing data from HuRI.

    input_type: is the event data structure that the ClientHook will process.
    It can be EventData or bytes, and must match event topic it react to.

    Class derived from ClientHook must implement hook.

    `singletton` is available to access and modifies shared ressources.
    """

    input_type: Type[T]

    def __init__(self, singletton: Any, **_):
        self.singletton = singletton

    async def hook(self, data: T):
        raise NotImplementedError


class Client:
    """Client is init with a Config, and connects to HuRI using websockets"""

    def __init__(
        self,
        config: ClientConfig,
    ):
        self.config = config

        module_path, object_name = self.config.interface_path.split(":", 1)

        module = importlib.import_module(module_path)
        interface = getattr(module, object_name)

        available_senders = interface.get_senders()
        self.senders: List[ClientSender] = [
            available_senders[sender.name](
                topic=sender.topic, singletton=interface.singletton, **sender.args
            )
            for sender in self.config.senders.values()
        ]

        available_hooks = interface.get_hooks()
        self.hooks: Dict[str, List[ClientHook]] = defaultdict(list)
        for hook in self.config.hooks.values():
            for topic in hook.topics:
                self.hooks[topic].append(
                    available_hooks[hook.name](
                        singletton=interface.singletton, **hook.args
                    )
                )

    @staticmethod
    def _log_hook_error(task: "asyncio.Task") -> None:
        """Surface hook failures.

        Hooks are fired as detached tasks, so an exception inside one is only
        reported by asyncio's "never retrieved" warning at GC — long after the
        fact, if at all. That is why a hook crashing on every chunk looks
        identical to a stream that was never sent.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    async def _receive_loop(self, ws: websockets.ClientConnection):
        try:
            while True:
                msg = await ws.recv()

                if isinstance(msg, bytes):
                    topic_len = struct.unpack(">H", msg[:2])[0]

                    topic = msg[2 : 2 + topic_len].decode()
                    data = msg[2 + topic_len :]

                else:
                    event = json.loads(msg)
                    topic = event["topic"]
                    data = event["data"]

                for hook in self.hooks[topic]:
                    # `from_wire` is the deserialization contract for BOTH wire
                    # shapes — Audio/Motion decode bytes, JsonEvent decodes the
                    # mapping. Handing binary topics the raw bytes instead left
                    # every hook typed for a bytes event (audio, motion)
                    # receiving a `bytes` and failing on attribute access.
                    hook_data = hook.input_type.from_wire(data)
                    task = asyncio.create_task(hook.hook(hook_data))
                    task.add_done_callback(self._log_hook_error)

        except (asyncio.CancelledError, websockets.ConnectionClosedOK):
            pass

    async def run(self):
        async with websockets.connect(self.config.huri_url) as ws:
            print("Connected to server")

            await ws.send(json.dumps(asdict(self.config)))

            try:
                init_msg = json.loads(await ws.recv())
            except websockets.ConnectionClosed:
                print(
                    "HuRI closed the connection without sending a session reply.\n"
                    "  Check the server is deployed: .huri-local/status.sh\n"
                    "  Replica errors: /tmp/ray/session_latest/logs/serve/"
                )
                return

            msg_type = init_msg.get("type")
            if msg_type == "session_error":
                print(f"HuRI rejected this client config: {init_msg.get('error')}")
                server_modules = init_msg.get("server_modules")
                if server_modules:
                    print(f"  server registered : {', '.join(server_modules)}")
                    print(f"  this config wants : {', '.join(self.config.modules)}")
                return
            if msg_type != "session_init":
                print(f"Unexpected first message from HuRI: {init_msg!r}")
                return

            user_id = init_msg["user_id"]
            print(f"Session started with _user_id: {user_id}")

            receive_task = asyncio.create_task(self._receive_loop(ws=ws))
            await asyncio.gather(
                *(sender.input_loop(ws=ws) for sender in self.senders),
            )

            receive_task.cancel()
