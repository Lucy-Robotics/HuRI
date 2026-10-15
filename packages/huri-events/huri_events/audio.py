import logging
import struct
from dataclasses import dataclass

import numpy as np

from huri_core.core.events import EventData

logger = logging.getLogger("ray.serve")


@dataclass
class Audio(EventData[bytes]):
    """Synthesized speech: float32 mono samples.

    Wire (binary): ``>IBd`` header — sample_rate (u32), end (u8), pts (f64,
    seconds from utterance start) — then the float32 samples.
    """

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
