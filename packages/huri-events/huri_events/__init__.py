"""The curated HuRI events, one class per `huri.*` topic.

| topic                   | class             | wire   |
|-------------------------|-------------------|--------|
| huri.audio.in           | BytesEvent (core) | binary |
| huri.voice              | Voice             | json   |
| huri.transcript         | Transcript        | json   |
| huri.emotion            | Emotion           | -      |
| huri.partial_question   | PartialQuestion   | json   |
| huri.question           | RAGQuestion       | json   |
| huri.token              | Token             | json   |
| huri.audio.out          | Audio             | binary |
| huri.motion             | Motion            | binary |

`Emotion` and `Voice` only travel between modules inside the server.
"""

from huri_events.audio import Audio
from huri_events.emotion import Emotion
from huri_events.motion import Motion
from huri_events.question import PartialQuestion, RAGQuestion
from huri_events.speech import Transcript, Voice
from huri_events.text import Token

__all__ = [
    "Audio",
    "Emotion",
    "Motion",
    "PartialQuestion",
    "RAGQuestion",
    "Token",
    "Transcript",
    "Voice",
]
