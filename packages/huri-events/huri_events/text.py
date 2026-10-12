from dataclasses import dataclass

from huri_core.core.events import JsonEvent


@dataclass
class Token(JsonEvent):
    """A delta of the assistant's streamed reply; `end` marks the end of the
    utterance (with empty text).

    Wire: JSON ``{"text": str, "end": bool}``.
    """

    text: str
    end: bool
