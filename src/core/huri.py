import json
import struct
import uuid
from typing import Dict, List, Type

from fastapi import WebSocket, WebSocketDisconnect
from ray import serve
from ray.serve import handle

from src.modules.factory import EventData, EventDataFactory, Module, ModuleFactory
from src.modules.utils.sender import Sender

from .app import app
from .dataclasses.config import ClientConfig
from .session import Session


@serve.deployment
@serve.ingress(app)
class HuRI:
    """
    Main HuRI realtime conversational AI server.

    This class manages realtime conversational sessions over FastAPI WebSockets
    and dynamically orchestrates modular AI pipelines using Ray Serve.

    Each client connection creates an isolated session containing a
    configurable pipeline of AI modules.
    """

    def __init__(
        self,
        modules: Dict[str, Type[Module]],
        handles: Dict[str, handle.DeploymentHandle],
        events: Dict[str, Type[EventData]],
    ) -> None:
        self.module_factory = ModuleFactory(handles)
        self.event_factory = EventDataFactory()
        for name, module_cls in modules.items():
            self.module_factory.register(name, module_cls)

            event_cls = events.pop(module_cls.input_type, None)
            self.event_factory.register(module_cls.input_type, event_cls)
            if module_cls.output_type is None:
                continue
            event_cls = events.pop(module_cls.output_type, None)
            self.event_factory.register(module_cls.output_type, event_cls)

        self.clients: Dict[str, Session] = {}

    @staticmethod
    def _check_subscriptions(pipeline: List[Module], topics: List[str]) -> None:
        produced = {m.output_type for m in pipeline if m.output_type is not None}
        unknown = sorted({t for t in topics if t not in produced})
        if unknown:
            raise ValueError(
                f"hook topic(s) {unknown} are not emitted by any module in this "
                f"session's pipeline "
                f"({sorted(type(m).__name__ for m in pipeline)}). "
                f"Subscribable topics: {sorted(produced)}"
            )

    @app.websocket("/session")
    async def run_session(self, ws: WebSocket):
        """
        Handle a realtime client session over WebSocket.

        This endpoint:
            - Accepts a WebSocket connection
            - Receives the client configuration
            - Dynamically instantiates requested modules
            - Creates a conversational session
            - Routes incoming events through the AI pipeline
            - Streams generated outputs back to the client

        Supported protocols:
            - JSON event messages
            - Binary framed messages

        JSON message example:
            {
                "topic": "text_in",
                "data": {...}
            }

        Binary packet structure:
            [topic_length][topic][payload]
        """
        await ws.accept()
        client_config_raw: Dict = await ws.receive_json()
        client_config = ClientConfig.from_dict(client_config_raw)

        topic_list = [
            topic
            for hook_config in client_config.hooks.values()
            for topic in hook_config.topics
        ]
        try:
            pipeline: List[Module] = self.module_factory.create_from_config(
                client_config.user_id, client_config.modules
            )
            self._check_subscriptions(pipeline, topic_list)
        except Exception as e:
            await ws.send_json(
                {
                    "type": "session_error",
                    "error": f"{type(e).__name__}: {e}",
                    "server_modules": self.module_factory.registered(),
                }
            )
            await ws.close(code=1008, reason="invalid session config")
            print(f"[HuRI] rejected session for {client_config.user_id}: {e}")
            return

        senders: List[Module] = [Sender(ws, topic) for topic in topic_list]
        modules: List[Module] = pipeline + senders

        await ws.send_json({"type": "session_init", "user_id": client_config.user_id})

        session_id = str(uuid.uuid4())
        self.clients[session_id] = Session(modules)
        print(f"Client registered with _user_id={client_config.user_id}, \
config: {client_config}")

        async def receive_loop(session: Session, ws: WebSocket):
            try:
                while True:
                    msg = await ws.receive()

                    if msg["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect()

                    if "bytes" in msg:
                        msg_bytes = msg["bytes"]
                        topic_len = struct.unpack("!H", msg_bytes[:2])[0]

                        topic = msg_bytes[2 : 2 + topic_len].decode()
                        data = msg_bytes[2 + topic_len :]
                    else:
                        msg_text = msg["text"]
                        event = json.loads(msg_text)
                        topic = event["topic"]
                        data = event["data"]  # TODO client/server one function

                    try:
                        event_data = self.event_factory.create(topic, data)
                    except RuntimeError as e:
                        print(
                            f"[HuRI] client {client_config.user_id}: dropping frame "
                            f"on topic {topic!r}: {e}. Known inbound topics: "
                            f"{self.event_factory.topics()}"
                        )
                        continue

                    await session.publish(topic, event_data)

            except RuntimeError as e:
                print(f"[ERROR] Client {client_config.user_id}:", e)
            except WebSocketDisconnect:
                pass
            finally:
                print(f"Client {client_config.user_id} disconnected")

        try:
            await receive_loop(self.clients[session_id], ws)
        finally:
            # Persist per-session state (e.g. conversation memory) on disconnect.
            for module in modules:
                fin = getattr(module, "finalize", None)
                if fin is None:
                    continue
                try:
                    await fin()
                except Exception:
                    import traceback

                    print(
                        f"[HuRI] finalize failed for {type(module).__name__}:\n"
                        f"{traceback.format_exc()}"
                    )
            self.clients.pop(session_id, None)
