"""Every curated event survives its wire format, and huri-events owns huri.*."""

from importlib.metadata import entry_points

import numpy as np
import pytest
from huri_events import (
    Audio,
    Emotion,
    Motion,
    PartialQuestion,
    RAGQuestion,
    Token,
    Transcript,
    Voice,
)

from huri_core.core.events import BytesEvent


def roundtrip(event):
    return type(event).from_wire(event.to_wire())


@pytest.mark.parametrize(
    "event",
    [
        Transcript("hello robot", True),
        Token("Hel", False),
        Token("", True),
        BytesEvent(b"\x00\x01\x02\x03"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_simple_events_roundtrip(event):
    assert roundtrip(event) == event


def test_question_roundtrips_with_and_without_emotion():
    emotion = Emotion("neu", 0.9, {"neu": 0.9, "ang": 0.1}, True)
    for question in (
        RAGQuestion(Transcript("hi", True), None),
        RAGQuestion(Transcript("hi", True), emotion),
    ):
        assert roundtrip(question) == question


def test_typed_question_shorthand():
    assert RAGQuestion.from_wire({"text": "hi"}) == RAGQuestion(Transcript("hi", True))


def test_audio_roundtrip_keeps_samples_and_timing():
    audio = Audio(np.linspace(-1, 1, 480, dtype=np.float32), 24000, True, 1.25)
    back = roundtrip(audio)
    assert (back.sample_rate, bool(back.end), back.pts) == (24000, True, 1.25)
    np.testing.assert_array_equal(back.data, audio.data)


def test_motion_roundtrip_and_truncation():
    frames = 3
    motion = Motion(
        poses=np.arange(frames * 165, dtype=np.float32).reshape(frames, 165),
        expressions=np.ones((frames, 100), dtype=np.float32),
        trans=np.zeros((frames, 3), dtype=np.float32),
        pts=0.5,
    )
    back = roundtrip(motion)
    np.testing.assert_array_equal(back.poses, motion.poses)
    assert (back.fps, back.pts) == (motion.fps, 0.5)
    with pytest.raises(ValueError, match="truncated"):
        Motion.from_wire(motion.to_wire()[:-4])


def test_internal_events_are_constructible():
    assert Voice(None).data is None
    assert PartialQuestion(Transcript("a", True), None).emotion is None


def test_huri_events_registers_every_curated_topic():
    eps = {ep.name: ep for ep in entry_points(group="huri.events")}
    curated = {name for name in eps if name.startswith("huri.")}
    assert curated == {
        "huri.audio.in",
        "huri.audio.out",
        "huri.voice",
        "huri.transcript",
        "huri.emotion",
        "huri.partial_question",
        "huri.question",
        "huri.token",
        "huri.motion",
    }
    dists = {getattr(eps[name].dist, "name", None) for name in curated}
    assert dists == {"huri-events"}
