from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.core.events import JsonEvent
from src.modules.emotion.events import Emotion
from src.modules.speech_to_text.events import Transcript


@dataclass
class RAGResult(JsonEvent):
    """What RAGHandle returns."""

    answer: str
    sources: list[dict] = field(default_factory=list)


@dataclass
class PartialQuestion(JsonEvent):
    """Partial question used to aggregate a sentence to an emotion."""

    transcript: Optional[Transcript]
    emotion: Optional[Emotion]


@dataclass
class RAGQuestion(JsonEvent):
    """Fully aggregated question to send to the RAG."""

    transcript: Transcript
    emotion: Optional[Emotion] = None

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "RAGQuestion":
        """Accept a typed question straight off the wire.

        In-pipeline, QAG builds this from a Transcript + Emotion. External clients
        (the website backend, the CLI text sender) inject a typed question as a
        bare ``{"text": ...}`` — no live emotion — which we wrap into a final
        Transcript. The nested branch also hydrates the dataclasses (JSON gives
        plain dicts) so a fully-shaped payload round-trips too.
        """
        if "text" in data:
            return cls(Transcript(text=data["text"], end=True), None)

        # Thomas: it's nasty, but ragquestion is a subclass and doesn't work well with
        # ray for my end, i get RAGQuestion.__init__() got an unexpected keyword
        # argument 'text' if there is a agnostic way, we can fix this, but until now
        # it's like this

        transcript = data.get("transcript")
        if isinstance(transcript, Mapping):
            transcript = Transcript(**transcript)
        if not isinstance(transcript, Transcript):
            raise ValueError(
                "RAGQuestion.from_wire needs a 'text' or 'transcript' field"
            )
        emotion = data.get("emotion")
        if isinstance(emotion, Mapping):
            emotion = Emotion(**emotion)
        return cls(transcript, emotion)
