from dataclasses import dataclass
from typing import Dict

from huri_core.core.events import EventData


@dataclass
class Emotion(EventData):
    """Speech emotion: best `label`, its `confidence`, all `scores`; `end`
    marks the utterance's final estimate. Travels between modules only
    (EMO -> EAG), it has no wire format."""

    label: str
    confidence: float
    scores: Dict[str, float]
    end: bool
