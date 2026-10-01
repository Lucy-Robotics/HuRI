"""Regression tests for the client<->server topic contract.

Renaming a topic in a module (`audio` -> `audio.out`) without updating the
client configs produced a pipeline that logged success at every stage and
delivered nothing: the EventGraph drops a publish with no subscriber, and an
outbound Sender bound to a topic nobody publishes simply never fires.
"""

import asyncio
import struct

import numpy as np
import pytest

from src.core.huri import HuRI as _HuRIDeployment
from src.core.module import Module
from src.core.session import Session
from src.modules.gesture.events import Motion
from src.modules.modules import get_modules
from src.modules.utils.sender import Sender

HuRI = _HuRIDeployment.func_or_class


class _Stub(Module):
    def __init__(self, input_type, output_type, emit=None):
        self.input_type = input_type
        self.output_type = output_type
        self._emit = emit

    async def process(self, data):
        return self._emit


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, payload: bytes):
        topic_len = struct.unpack(">H", payload[:2])[0]
        self.sent.append(
            (payload[2 : 2 + topic_len].decode(), payload[2 + topic_len :])
        )

    async def send_json(self, message):
        self.sent.append((message["topic"], message["data"]))


def _motion(frames=3):
    return Motion(
        poses=np.arange(frames * 165, dtype=np.float32).reshape(frames, 165),
        expressions=np.arange(frames * 100, dtype=np.float32).reshape(frames, 100),
        trans=np.arange(frames * 3, dtype=np.float32).reshape(frames, 3),
        pts=1.5,
    )


def test_pipeline_topics_are_connected():
    """Every module's input must be some other module's output (or an entrypoint)."""
    modules = get_modules()
    produced = {m.output_type for m in modules.values() if m.output_type}
    entrypoints = {"audio.in", "question"}  # fed by the client, not by a module
    dangling = {
        name: m.input_type
        for name, m in modules.items()
        if m.input_type not in produced and m.input_type not in entrypoints
    }
    assert not dangling, f"modules subscribed to topics nobody emits: {dangling}"


def test_unknown_hook_topic_is_rejected():
    pipeline = [_Stub("token", "audio.out")]
    with pytest.raises(ValueError, match="audio.out"):
        # the pre-rename name: must not silently produce a dead Sender
        HuRI._check_subscriptions(pipeline, ["audio"])


def test_known_hook_topic_is_accepted():
    HuRI._check_subscriptions([_Stub("token", "audio.out")], ["audio.out"])


def test_event_reaches_the_sender_for_its_topic():
    ws = _FakeWS()
    motion = _motion()
    session = Session(
        [
            _Stub("token", "audio.out", emit=None),
            _Stub("audio.out", "motion", emit=motion),
            Sender(ws, "motion"),
        ]
    )

    async def run():
        await session.publish("audio.out", object())
        for _ in range(50):
            await asyncio.sleep(0.005)
            if ws.sent:
                return

    asyncio.run(run())
    assert [topic for topic, _ in ws.sent] == ["motion"]


def test_motion_survives_the_wire():
    """from_wire used to drop the body, yielding a zero-frame Motion."""
    original = _motion(4)
    decoded = Motion.from_wire(original.to_wire())

    assert decoded.pts == pytest.approx(original.pts)
    assert decoded.fps == original.fps
    np.testing.assert_array_equal(decoded.poses, original.poses)
    np.testing.assert_array_equal(decoded.expressions, original.expressions)
    np.testing.assert_array_equal(decoded.trans, original.trans)
