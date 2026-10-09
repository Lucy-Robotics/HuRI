import os
from typing import Dict, Type

from src.modules.emotion.emotion_aggregator import EAG
from src.modules.emotion.prosody_analysis import EMO
from src.modules.gesture.gesture import MOV
from src.modules.rag.question_aggregator import QAG
from src.modules.rag.rag import RAG
from src.modules.speech_to_text.microphone_vad import VAD
from src.modules.speech_to_text.speech_to_text import STT
from src.modules.speech_to_text.text_aggregator import TAG
from src.modules.text_to_speech.text_to_speech import TTS

from .factory import Module


def get_modules() -> Dict[str, Type[Module]]:
    modules: Dict[str, Type[Module]] = {
        "vad": VAD,
        "stt": STT,
        "tag": TAG,
        "emo": EMO,
        "eag": EAG,
        "qag": QAG,
        "rag": RAG,
        "tts": TTS,
        "mov": MOV,
    }

    # Optional allow-list. HURI_MODULES="stt,rag,tts" restricts which modules
    # this server registers — and therefore which Serve deployments get built by
    # bind_deployment_handles(), so a host that cannot run e.g. CosyVoice never
    # creates the TTS replica. Unset (the default, and what the Helm charts do)
    # keeps every module. scripts/install_local.sh sets it from its hardware plan.
    selection = os.environ.get("HURI_MODULES", "").strip()
    if selection:
        wanted = {name.strip() for name in selection.split(",") if name.strip()}
        unknown = wanted - modules.keys()
        if unknown:
            raise ValueError(
                f"HURI_MODULES lists unknown module(s): {sorted(unknown)}. "
                f"Known modules: {sorted(modules)}"
            )
        modules = {name: cls for name, cls in modules.items() if name in wanted}

    return modules
