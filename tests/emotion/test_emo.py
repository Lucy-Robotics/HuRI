import asyncio

import numpy as np

from src.modules.speech_to_text.events import Voice


class FakeHandle:
    """Returns a fixed emotion per call; can block the first call."""

    def __init__(self, gate=None):
        self.gate = gate
        self.calls = []
        self.predict = self

    async def remote(self, audio):
        self.calls.append(len(audio))
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()
        return {"label": "neu", "confidence": 0.9, "scores": {"neu": 0.9, "ang": 0.1}}


def _emo(handle):
    from src.modules.emotion.prosody_analysis import EMO

    # 20 ms blocks -> window of 10 frames (fast tests).
    return EMO(_handle=handle, analysis_window=0.2)


async def _collect(emo, voice):
    return [e async for e in emo.process(voice)]


def _voice():
    return Voice(np.zeros(4, dtype=np.float32))


def test_interim_emotion_once_window_is_full():
    asyncio.run(_test_interim_emotion_once_window_is_full_impl())


async def _test_interim_emotion_once_window_is_full_impl():
    handle = FakeHandle()
    emo = _emo(handle)

    results = []
    for _ in range(10):
        results += await _collect(emo, _voice())

    assert [e.end for e in results] == [False]
    assert handle.calls == [40]


def test_end_marker_always_yields_final_emotion():
    asyncio.run(_test_end_marker_always_yields_final_emotion_impl())


async def _test_end_marker_always_yields_final_emotion_impl():
    handle = FakeHandle()
    emo = _emo(handle)

    # Very short turn: no frames at all still produces Emotion(end=True).
    results = await _collect(emo, Voice(None))

    assert [e.end for e in results] == [True]
    assert results[0].label == "neu"


def test_end_marker_during_inflight_analysis_is_not_dropped():
    asyncio.run(_test_end_marker_during_inflight_analysis_is_not_dropped_impl())


async def _test_end_marker_during_inflight_analysis_is_not_dropped_impl():
    gate = asyncio.Event()
    handle = FakeHandle(gate)
    emo = _emo(handle)

    for _ in range(9):
        await _collect(emo, _voice())

    # 10th frame starts an analysis that blocks on the gate.
    inflight = asyncio.create_task(_collect(emo, _voice()))
    await asyncio.sleep(0)

    # The single end marker lands mid-analysis: it must be remembered.
    assert await _collect(emo, Voice(None)) == []
    gate.set()

    results = await inflight
    assert [e.end for e in results] == [False, True]
