"""Regression tests for the client<->server topic contract.

Renaming a topic in a module (`audio` -> `audio.out`) without updating the
client configs produced a pipeline that logged success at every stage and
delivered nothing: the EventGraph drops a publish with no subscriber, and an
outbound Sender bound to a topic nobody publishes simply never fires.

The events here are toys: the curated topics and their wire formats are
tested in huri-modules, next to huri-events.
"""

import asyncio
import struct
from dataclasses import dataclass

import pytest

from huri_core.core.compat import CompatibilityError, check_session
from huri_core.core.events import BytesEvent, JsonEvent
from huri_core.core.module import Module
from huri_core.core.sender import Sender
from huri_core.core.session import Session


@dataclass
class Pose(JsonEvent):
    joints: list
    pts: float


class Frame(BytesEvent):
    """A binary event, framed `[u16 topic length][topic][payload]` on the wire."""


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


def _deliver(session, topic, ws):
    async def run():
        await session.publish(topic, object())
        for _ in range(50):
            await asyncio.sleep(0.005)
            if ws.sent:
                return

    asyncio.run(run())


def test_unknown_hook_topic_is_rejected():
    pipeline = [_Stub("toy.token", "toy.audio.out")]
    with pytest.raises(CompatibilityError, match="toy.audio.out"):
        # the pre-rename name: must not silently produce a dead Sender
        check_session(pipeline, sent=["toy.token"], subscribed=["audio"])


def test_known_hook_topic_is_accepted():
    check_session(
        [_Stub("toy.token", "toy.audio.out")],
        sent=["toy.token"],
        subscribed=["toy.audio.out"],
    )


def test_json_event_reaches_the_sender_for_its_topic():
    ws = _FakeWS()
    session = Session(
        [
            _Stub("toy.token", "toy.audio.out", emit=None),
            _Stub("toy.audio.out", "toy.pose", emit=Pose([0.1, 0.2], pts=1.5)),
            Sender(ws, "toy.pose"),
        ]
    )
    _deliver(session, "toy.audio.out", ws)
    assert ws.sent == [("toy.pose", {"joints": [0.1, 0.2], "pts": 1.5})]


def test_bytes_event_is_sent_as_a_framed_binary_message():
    ws = _FakeWS()
    session = Session(
        [
            _Stub("toy.audio.out", "toy.frame", emit=Frame(b"\x00\x01\x02")),
            Sender(ws, "toy.frame"),
        ]
    )
    _deliver(session, "toy.audio.out", ws)
    assert ws.sent == [("toy.frame", b"\x00\x01\x02")]
