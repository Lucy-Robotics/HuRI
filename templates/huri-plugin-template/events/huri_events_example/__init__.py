from dataclasses import dataclass

from huri_core.core.events import JsonEvent


@dataclass
class Shout(JsonEvent):
    """What the example module emits: the transcript, louder.

    Wire: JSON ``{"text": str}``. Document the wire format here — it is the
    contract with every client that decodes this topic.
    """

    text: str
