"""MOV's audio windowing, with EMAGE replaced by a fake deployment."""

import asyncio

import numpy as np
from huri_events.audio import Audio
from huri_events.motion import Motion
from huri_mov.module import MOV

from huri_core.testing import LocalHandle, ModuleHarness

SR = 16000


class FakeEmage:
    """30 fps of zero poses covering the whole window it is given."""

    def __init__(self):
        self.windows = []

    async def infer(self, window, sr):
        self.windows.append(len(window))
        frames = int(round(len(window) / sr * 30))
        return Motion(
            poses=np.zeros((frames, 165), dtype=np.float32),
            expressions=np.zeros((frames, 100), dtype=np.float32),
            trans=np.zeros((frames, 3), dtype=np.float32),
        )


def chunk(seconds, end=False):
    return Audio(np.zeros(int(seconds * SR), dtype=np.float32), SR, end)


def mov(**args):
    emage = FakeEmage()
    return emage, ModuleHarness(MOV, handle=LocalHandle(emage), **args)


def test_waits_for_enough_fresh_audio_before_inferring():
    emage, harness = mov(min_chunk_sec=0.5, context_sec=1.0)
    assert harness.run(chunk(0.2), chunk(0.2)) == []
    assert emage.windows == []
    (motion,) = harness.run(chunk(0.2))  # 0.6 s accumulated
    assert motion.poses.shape[0] == 18


def test_end_of_utterance_flushes_the_tail_and_resets():
    emage, harness = mov(min_chunk_sec=0.5, context_sec=1.0)

    async def utterance():
        out = await harness.feed_all(chunk(0.6), chunk(0.2, end=True))
        return out, harness.module._sr

    out, sample_rate = asyncio.run(utterance())
    assert [m.poses.shape[0] for m in out] == [18, 6]  # only the new 0.2 s
    assert emage.windows[1] == int(0.8 * SR)  # inferred with context
    assert sample_rate is None  # ready for the next utterance
