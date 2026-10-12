from huri_tts import StreamingTTS
from huri_tts_cosyvoice.handle import CosyVoiceTTSHandle


class CosyVoiceTTS(StreamingTTS):
    """Text to speech with CosyVoice3: zero-shot voice cloning from a reference
    sample (HURI_VOICE_SAMPLE_PATH + HURI_VOICE_TRANSCRIPT). Needs an NVIDIA GPU
    to run in real time, and the CosyVoice sources on PYTHONPATH
    (install_cosyvoice.sh)."""

    _handle_cls = CosyVoiceTTSHandle
