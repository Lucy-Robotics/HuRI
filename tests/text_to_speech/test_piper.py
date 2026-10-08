import asyncio
import os

import pytest

from src.modules.text_to_speech import piper_tts

pytestmark = pytest.mark.skipif(
    not os.path.isfile(piper_tts._VOICE_PATH),
    reason="no Piper voice (set HURI_PIPER_VOICE)",
)


async def _speak(handle, session_id, text):
    await handle.start_session(session_id)
    await handle.push_text(session_id, text, end=True)
    return [audio async for audio in handle.stream_audio(session_id)]


def test_same_phrase_can_be_spoken_twice():
    # onnxruntime's memory-pattern planner caches buffers per input shape, but
    # Piper's output length varies run to run: the second phrase with the same
    # phoneme count used to crash the model and drop the sentence.
    handle = piper_tts.PiperTTSHandle.func_or_class()

    for i in range(3):
        chunks = asyncio.run(_speak(handle, f"s{i}", "Sure thing!"))
        assert chunks[-1].end
        assert sum(c.data.size for c in chunks) > 0
