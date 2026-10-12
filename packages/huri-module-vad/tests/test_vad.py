"""VAD's speech/silence logic, with WebRTC's detector scripted."""

import numpy as np
import pytest
from huri_events.speech import Voice
from huri_vad.module import VAD

from huri_core.core.events import BytesEvent
from huri_core.testing import ModuleHarness

FRAME = (np.full(320, 16384, dtype=np.int16)).tobytes()  # 20 ms at 16 kHz


class Scripted:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def is_speech(self, frame, sample_rate):
        return self.decisions.pop(0)


def vad(decisions, silence_duration=0.05):
    harness = ModuleHarness(VAD, silence_duration=silence_duration)
    harness.module.vad = Scripted(decisions)
    return harness


def test_speech_frames_become_normalized_voice():
    (voice,) = vad([True]).run(BytesEvent(FRAME))
    assert voice.data.dtype == np.float32 and voice.data.size == 320
    assert voice.data[0] == pytest.approx(0.5)


def test_end_of_utterance_is_sent_once_after_enough_silence():
    # 0.05 s of silence = more than 2 frames of 20 ms.
    out = vad([True] + [False] * 6).run(*[BytesEvent(FRAME)] * 7)
    assert [v.data is None for v in out] == [False, True]


def test_silence_before_any_speech_sends_nothing():
    assert vad([False] * 5).run(*[BytesEvent(FRAME)] * 5) == []


def test_block_duration_must_be_one_webrtc_accepts():
    with pytest.raises(RuntimeError, match="block duration"):
        ModuleHarness(VAD, block_duration=0.025)


def test_real_detector_runs_on_a_frame():
    out = ModuleHarness(VAD).run(BytesEvent(bytes(640)))  # digital silence
    assert out == [] or isinstance(out[0], Voice)
