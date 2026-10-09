from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.core.events import JsonEvent


@dataclass
class Transcript(JsonEvent):
    text: str
    end: bool


@dataclass
class Voice(JsonEvent):
    data: Optional[np.ndarray]

    def summarize(self) -> str:
        """Short repr that avoids dumping full numpy arrays into the log."""

        if self.data is not None:
            cls = type(self).__name__
            return f"{cls}(shape={self.data.shape}, dtype={self.data.dtype})"
        else:
            return super().summarize()
