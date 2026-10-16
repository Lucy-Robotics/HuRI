import logging
import struct

from fastapi import WebSocket

from huri_core.core.events import EventData
from huri_core.core.module import Module

logger = logging.getLogger("ray.serve")


class Sender(Module):
    """Sender Module

    Sends the events of one topic (`input_type`) to the client, on the wire
    the event's to_wire() chooses:
        - a mapping (JsonEvent): a JSON text frame {"topic": ..., "data": ...};
        - bytes (BytesEvent): a binary frame [u16 topic length][topic][payload].
    The payload's own layout (audio, motion...) is the event package's
    business: see its to_wire().

    input: auto,
    output: None"""

    output_type = None

    def __init__(self, ws: WebSocket, type: str):
        super().__init__()
        self.ws: WebSocket = ws
        self.input_type = type

    async def process(self, data: EventData):
        logger.info("[Sender:%s] received %s", self.input_type, type(data).__name__)

        wire = data.to_wire()
        if isinstance(wire, bytes):
            await self.ws.send_bytes(self._prefix(wire))
        else:
            await self.ws.send_json(
                {
                    "topic": self.input_type,
                    "data": wire,
                }
            )

    def _prefix(self, payload: bytes) -> bytes:
        """Encode topic and topic len and adds it as a prefix to the payload"""
        topic_bytes = self.input_type.encode()
        return struct.pack(">H", len(topic_bytes)) + topic_bytes + payload
