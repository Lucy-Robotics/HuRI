"""A terminal view of the gesture stream (`huri.motion`).

Each Motion chunk carries SMPL-X poses: 55 joints x 3 axis-angle values per
frame. The viewer groups the joints by body part and shows how much each part
moves (mean absolute change of its joint rotations between frames), so one can
see at a glance whether gestures follow the speech. Optionally every chunk is
saved as .npz for offline rendering with the EMAGE/SMPL-X tooling.
"""

import logging
import os
from typing import Dict, List, Optional, Sequence

import numpy as np

from huri_core.modules.gesture.events import Motion

logger = logging.getLogger("huri_cli.motion")

# SMPL-X joint indices (55-joint body + jaw/eyes + hands layout).
BODY_PARTS: Dict[str, Sequence[int]] = {
    "head": (12, 15, 22, 23, 24),
    "torso": (0, 3, 6, 9),
    "l_arm": (13, 16, 18, 20),
    "r_arm": (14, 17, 19, 21),
    "hands": tuple(range(25, 55)),
    "legs": (1, 2, 4, 5, 7, 8, 10, 11),
}
_BAR = " ▁▂▃▄▅▆▇█"


def part_activity(
    poses: np.ndarray, previous: Optional[np.ndarray] = None
) -> Dict[str, float]:
    """Mean absolute rotation change per frame (radians) of each body part."""

    frames = poses.reshape(len(poses), 55, 3)
    if previous is not None:
        frames = np.concatenate([previous.reshape(1, 55, 3), frames])
    if len(frames) < 2:
        return {part: 0.0 for part in BODY_PARTS}
    delta = np.abs(np.diff(frames, axis=0))  # (t-1, 55, 3)
    return {
        part: float(delta[:, list(joints), :].mean())
        for part, joints in BODY_PARTS.items()
    }


def bar(value: float, full_scale: float = 0.05) -> str:
    level = min(int(value / full_scale * (len(_BAR) - 1)), len(_BAR) - 1)
    return _BAR[max(level, 0)]


class MotionViewer:
    """Logs one line per Motion chunk: timing plus a bar per body part."""

    def __init__(self, save_motion_dir: str = "", full_scale: float = 0.05):
        self.full_scale = full_scale
        self.save_motion_dir = save_motion_dir
        self._last_pose: Optional[np.ndarray] = None
        self._index = 0
        if save_motion_dir:
            os.makedirs(save_motion_dir, exist_ok=True)

    def render(self, motion: Motion) -> str:
        activity = part_activity(motion.poses, self._last_pose)
        parts: List[str] = [
            f"{part} {bar(value, self.full_scale)}" for part, value in activity.items()
        ]
        n = motion.poses.shape[0]
        return (
            f"motion pts={motion.pts:7.3f}s {n:3d}f @{motion.fps}fps "
            f"({n / max(motion.fps, 1):.2f}s) | " + "  ".join(parts)
        )

    async def __call__(self, motion: Motion) -> None:
        if motion.poses.shape[0] == 0:
            return
        logger.info(self.render(motion))
        self._last_pose = motion.poses[-1]
        if self.save_motion_dir:
            path = os.path.join(self.save_motion_dir, f"motion-{self._index:04d}.npz")
            np.savez(
                path,
                poses=motion.poses,
                expressions=motion.expressions,
                trans=motion.trans,
                fps=motion.fps,
                pts=motion.pts,
            )
            self._index += 1
