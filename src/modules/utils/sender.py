import logging
import struct

from fastapi import WebSocket

from src.core.events import EventData
from src.core.module import Module

logger = logging.getLogger("ray.serve")


class Sender(Module):
    """Sender Module

    Send output data to the client.
    This data must be JSON serialisable, like a dataclass.
    Audio wire format:  [4B sample_rate uint32][1B end][8B pts float64][float32 PCM].
    Motion wire format: [8B pts float64][4B fps uint32][4B n_frames uint32]
                        [poses float32 n*165][expressions float32 n*100]
                        [trans float32 n*3].

    This data must be JSON serialisable, like a dataclass.

    Audio wire format:
        [4B sample_rate uint32][1B end][8B pts float64][float32 PCM].
    Motion wire format:
        [8B pts float64][4B fps uint32][4B n_frames uint32]
        [poses float32 n*165][expressions float32 n*100][trans float32 n*3].

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
