import logging
import struct
from dataclasses import dataclass

import numpy as np

from src.core.events import EventData, JsonEvent

logger = logging.getLogger("ray.serve")


@dataclass
class Token(JsonEvent):
    text: str
    end: bool


@dataclass
class Audio(EventData[bytes]):
    data: np.ndarray
    sample_rate: int
    end: bool = False
    pts: float = 0.0  # presentation timestamp in seconds from utterance start

    def to_wire(self) -> bytes:
        logger.info(
            "Audio samples=%d sr=%d end=%s pts=%.3fs",
            self.data.shape[0],
            self.sample_rate,
            self.end,
            self.pts,
        )
        header = struct.pack(">IBd", self.sample_rate, int(self.end), self.pts)
        return header + bytes(self.data.tobytes())

    @classmethod
    def from_wire(cls, data: bytes) -> "Audio":
        sample_rate, end, pts = struct.unpack(">IBd", data[:13])
        samples = np.frombuffer(data[13:], dtype=np.float32)

        return cls(sample_rate=sample_rate, end=end, pts=pts, data=samples)

    def summarize(self) -> str:
        """Short repr that avoids dumping full numpy arrays into the log."""

        cls = type(self).__name__
        return f"{cls}(shape={self.data.shape}, dtype={self.data.dtype})"
