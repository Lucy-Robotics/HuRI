from typing import Dict, Type

from src.core.events import BytesEvent, EventData
from src.modules.emotion.events import Emotion
from src.modules.gesture.events import Motion
from src.modules.rag.events import PartialQuestion, RAGQuestion
from src.modules.speech_to_text.events import Transcript, Voice
from src.modules.text_to_speech.events import Audio, Token


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
