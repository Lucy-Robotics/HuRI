from huri_tts import StreamingTTS
from huri_tts_piper.handle import PiperTTSHandle


class PiperTTS(StreamingTTS):
    """Text to speech with Piper (ONNX): CPU-only, ~30x faster than realtime.

    The default engine: it runs on any machine. See handle.py for why.
    """

    _handle_cls = PiperTTSHandle
