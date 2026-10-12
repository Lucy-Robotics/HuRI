from typing import Dict, Type

from huri_core.core.events import BytesEvent, EventData
from huri_core.modules.emotion.events import Emotion
from huri_core.modules.gesture.events import Motion
from huri_core.modules.rag.events import PartialQuestion, RAGQuestion
from huri_core.modules.speech_to_text.events import Transcript, Voice
from huri_core.modules.text_to_speech.events import Audio, Token


def get_events() -> Dict[str, Type[EventData]]:
    events: Dict[str, Type[EventData]] = {
        "audio.in": BytesEvent,  # inbound mic frames (raw int16 PCM)
        "audio.out": Audio,
        "voice": Voice,
        "transcript": Transcript,
        "emotion": Emotion,
        "partial_question": PartialQuestion,
        "question": RAGQuestion,
        "token": Token,
        "motion": Motion,
    }

    return events
