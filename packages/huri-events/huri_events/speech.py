from dataclasses import dataclass
from typing import Optional

import numpy as np

from huri_core.core.events import JsonEvent


@dataclass
class Transcript(JsonEvent):
    """Text recognized from speech; `end` closes the utterance.

    Wire: JSON ``{"text": str, "end": bool}``.
    """

    text: str
    end: bool


@dataclass
class Voice(JsonEvent):
    """A frame of detected speech (float32 samples), or `data=None` to mark the
    end of the utterance. Travels between modules only (VAD -> STT, EMO)."""

    data: Optional[np.ndarray]

    def summarize(self) -> str:
        """Short repr that avoids dumping full numpy arrays into the log."""

        if self.data is not None:
            cls = type(self).__name__
            return f"{cls}(shape={self.data.shape}, dtype={self.data.dtype})"
        else:
            return super().summarize()
