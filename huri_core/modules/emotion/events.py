from dataclasses import dataclass
from typing import Dict

from huri_core.core.events import EventData


@dataclass
class Emotion(EventData):
    label: str
    confidence: float
    scores: Dict[str, float]
    end: bool
