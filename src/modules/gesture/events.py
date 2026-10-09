import logging
import struct
from dataclasses import dataclass

import numpy as np

from src.core.events import EventData

_EMAGE_FPS = 30

# Layout of the per-frame vectors, shared by to_wire/from_wire.
_POSE_DIM = 165  # 55 SMPL-X joints x 3 axis-angle
_EXPR_DIM = 100  # facial expression coefficients
_TRANS_DIM = 3  # global root translation

logger = logging.getLogger("ray.serve")


@dataclass
class Motion(EventData[bytes]):
    poses: np.ndarray  # (t, 165)  SMPL-X axis-angle, 55 joints × 3
    expressions: np.ndarray  # (t, 100)  facial expression coefficients
    trans: np.ndarray  # (t, 3)    global root translation
    fps: int = _EMAGE_FPS
    pts: float = 0.0  # presentation timestamp in seconds, paired with Audio.pts

    def to_wire(self) -> bytes:
        n_frames = self.poses.shape[0]
        logger.info(
            "Motion frames=%d fps=%d pts=%.3fs",
            n_frames,
            self.fps,
            self.pts,
        )
        header = struct.pack(">dII", self.pts, self.fps, n_frames)
        body: bytes = (
            self.poses.astype(np.float32).tobytes()
            + self.expressions.astype(np.float32).tobytes()
            + self.trans.astype(np.float32).tobytes()
        )
        return header + body

    def summarize(self) -> str:
        cls = type(self).__name__
        return (
            f"{cls}(frames={self.poses.shape[0]}, "
            f"fps={self.fps}, pts={self.pts:.3f}s)"
        )

    @classmethod
    def from_wire(cls, data: bytes) -> "Motion":
        pts, fps, n_frames = struct.unpack(">dII", data[:16])

        body = np.frombuffer(data[16:], dtype=np.float32)
        expected = n_frames * (_POSE_DIM + _EXPR_DIM + _TRANS_DIM)
        if body.size < expected:
            raise ValueError(
                f"truncated motion frame: {body.size} floats, expected {expected} "
                f"for {n_frames} frames"
            )

        offset = 0
        out = []
        for dim in (_POSE_DIM, _EXPR_DIM, _TRANS_DIM):
            out.append(body[offset : offset + n_frames * dim].reshape(n_frames, dim))
            offset += n_frames * dim
        poses, expressions, trans = out

        return cls(
            poses=poses,
            expressions=expressions,
            trans=trans,
            fps=fps,
            pts=pts,
        )
