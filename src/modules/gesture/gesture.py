import asyncio
import os
from typing import AsyncGenerator, Optional

import numpy as np
from ray import serve
from ray.serve import handle

from src.core.module import ModuleWithHandle
from src.modules.gesture.events import Motion
from src.modules.text_to_speech.events import Audio

_HF_REPO = os.environ.get("HURI_EMAGE_REPO", "H-Liu1997/emage_audio")
_EMAGE_SR = 16000  # EMAGE expects 16 kHz mono audio

# Sliding-window defaults. Overridable per-deployment via the module `args`
# block in the client config, or globally via the env vars below.
_CONTEXT_SEC = float(os.environ.get("HURI_GESTURE_CONTEXT_SEC", "2.0"))
_MIN_CHUNK_SEC = float(os.environ.get("HURI_GESTURE_MIN_CHUNK_SEC", "0.5"))

# Seconds over which a fresh window's first frames are eased onto the last
# emitted pose, killing the seam snap between windows and between utterances.
_BLEND_SEC = float(os.environ.get("HURI_GESTURE_BLEND_SEC", "0.2"))

# Optional manual GPU split: cap the gesture process to a fraction of the GPU so
# TTS keeps the lion's share. Only applied on CUDA when the value is set (>0).
_GPU_MEM_FRACTION = float(os.environ.get("HURI_GESTURE_GPU_MEM_FRACTION", "0.0"))

# Source sample rate used to warm the inference path. Real audio arrives from
# the TTS (CosyVoice ≈ 24 kHz), so every real infer() call resamples to 16 kHz.
# Warming at 16 kHz — as the old warmup did — skips that resample entirely,
# leaving librosa's first-call cost to land on the first user-facing gesture.
# Default to the TTS rate so the resample path is warmed too. Override if your
# TTS uses a different rate (the exact value only affects which resampler
# filter is pre-built; the model shapes follow the 16 kHz duration regardless).
_WARMUP_SRC_SR = int(os.environ.get("HURI_GESTURE_WARMUP_SR", "24000"))


@serve.deployment(name="MOVHandle")
class MOVHandle:
    def __init__(
        self,
        hf_repo: str = _HF_REPO,
        device: Optional[str] = None,
        gpu_mem_fraction: float = _GPU_MEM_FRACTION,
    ):
        print("[MOV] importing torch...")
        import torch

        # Pin algorithm selection so the kernels warmed below are the same ones
        # used at serve time. With cudnn.benchmark enabled, cuDNN re-autotunes
        # for every new input length — and the sliding window feeds a different
        # length almost every call — so the first inference at each new shape
        # would stall on autotuning, defeating the warmup. Keep it off (also the
        # default) and pin it explicitly. TF32 just speeds matmul/conv on
        # Ampere+ with no meaningful quality impact for gesture.
        torch.backends.cudnn.benchmark = False
        if torch.cuda.is_available():
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True

        print("[MOV] importing emage...")
        from .emage import EmageAudioModel, EmageVAEConv, EmageVQModel, EmageVQVAEConv

        self.device = torch.device(
            device if device else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        print(f"[MOV] device={self.device} hf_repo={hf_repo!r}")

        # Manual GPU split: cap this process' share of GPU memory so the audio
        # (TTS) path keeps the rest. num_gpus in the Ray serveConfig handles
        # scheduling/packing; this caps actual allocation on the device.
        if self.device.type == "cuda" and gpu_mem_fraction > 0:
            try:
                torch.cuda.set_per_process_memory_fraction(
                    gpu_mem_fraction, self.device.index or 0
                )
                print(
                    f"[MOV] GPU memory fraction capped at {gpu_mem_fraction:.2f}",
                )
            except Exception as e:  # noqa: BLE001 — best-effort knob, never fatal
                print(f"[MOV] WARNING could not cap GPU memory: {e!r}")

        print("[MOV] loading face_vq...")
        face_vq = EmageVQVAEConv.from_pretrained(hf_repo, subfolder="emage_vq/face").to(
            self.device
        )
        print("[MOV] loading upper_vq...")
        upper_vq = EmageVQVAEConv.from_pretrained(
            hf_repo, subfolder="emage_vq/upper"
        ).to(self.device)
        print("[MOV] loading lower_vq...")
        lower_vq = EmageVQVAEConv.from_pretrained(
            hf_repo, subfolder="emage_vq/lower"
        ).to(self.device)
        print("[MOV] loading hands_vq...")
        hands_vq = EmageVQVAEConv.from_pretrained(
            hf_repo, subfolder="emage_vq/hands"
        ).to(self.device)
        print("[MOV] loading global_ae...")
        global_ae = EmageVAEConv.from_pretrained(
            hf_repo, subfolder="emage_vq/global"
        ).to(self.device)

        self.motion_vq = EmageVQModel(
            face_model=face_vq,
            upper_model=upper_vq,
            lower_model=lower_vq,
            hands_model=hands_vq,
            global_model=global_ae,
        )
        self.motion_vq.eval()

        print("[MOV] loading EmageAudioModel...")
        self.model = EmageAudioModel.from_pretrained(hf_repo).to(self.device)
        self.model.eval()

        self._warmup()
        print("[MOV] ready")

    def _warmup(self) -> None:
        # The first inference pays one-time costs that are *shape- and
        # path-dependent*: per-input-length kernel/primitive selection (cuDNN
        # algo pick on GPU, oneDNN primitive build on CPU), librosa's first-call
        # resampler build, the caching allocator's first growth, and CUDA
        # context/kernel load. The old warmup ran a single 16 kHz, fixed-length,
        # no-resample pass — so it warmed exactly one shape on a path real calls
        # never take. The first real gesture (a different length, arriving at the
        # TTS rate and therefore resampled) re-paid almost all of it, which is
        # why the warmup "did nothing".
        #
        # Instead, sweep the window lengths the sliding window actually feeds
        # infer() — from the small first-chunk window up to a full context+chunk
        # steady-state window — on the *real* resample path, twice (the first
        # pass pays the costs, the second confirms the path is hot), and
        # synchronize so the GPU work is finished before we report ready.
        # Best-effort: a failure here must never prevent the deployment coming up.
        import time

        import torch

        # Representative window lengths (seconds). The dominant per-window
        # transformer forward is a fixed shape warmed by any window, but the
        # trailing remainder forward varies with total length, so warm a spread.
        secs = sorted(
            {
                round(s, 3)
                for s in (
                    _MIN_CHUNK_SEC,  # first tiny window of an utterance
                    _CONTEXT_SEC,  # context-only sized window
                    _CONTEXT_SEC + _MIN_CHUNK_SEC,  # steady-state window
                    _CONTEXT_SEC + 2 * _MIN_CHUNK_SEC,  # a larger fresh chunk
                )
                if s and s > 0
            }
        ) or [3.0]

        try:
            t0 = time.time()
            for pass_idx in range(2):
                for s in secs:
                    n = max(1, int(_WARMUP_SRC_SR * s))
                    dummy = np.zeros(n, dtype=np.float32)
                    ts = time.time()
                    self.infer(dummy, source_sr=_WARMUP_SRC_SR)
                    if self.device.type == "cuda":
                        torch.cuda.synchronize(self.device)
                    print(
                        f"[MOV] warmup pass {pass_idx} {s:.2f}s "
                        f"({n} samples @ {_WARMUP_SRC_SR} Hz) "
                        f"in {time.time() - ts:.2f}s",
                    )
            print(
                f"[MOV] warmup done ({len(secs)} shapes x2) "
                f"in {time.time() - t0:.2f}s",
            )
        except Exception as e:  # noqa: BLE001 — warmup is an optimisation, never fatal
            print(f"[MOV] WARNING warmup failed: {e!r}")

    def infer(self, audio_np: np.ndarray, source_sr: int = _EMAGE_SR) -> Motion:
        import torch
        import torch.nn.functional as F

        if source_sr != _EMAGE_SR:
            import librosa

            audio_np = librosa.resample(
                audio_np, orig_sr=source_sr, target_sr=_EMAGE_SR
            )

        audio_ts = torch.from_numpy(audio_np).to(self.device).unsqueeze(0)
        speaker_id = torch.zeros(1, 1, dtype=torch.long, device=self.device)

        with torch.no_grad():
            ref_trans = torch.zeros(1, 1, 3, device=self.device)
            latent_dict = self.model.inference(audio_ts, speaker_id, self.motion_vq)

            cfg = self.model.cfg
            face_latent = (
                latent_dict["rec_face"] if cfg.lf > 0 and cfg.cf == 0 else None
            )
            upper_latent = (
                latent_dict["rec_upper"] if cfg.lu > 0 and cfg.cu == 0 else None
            )
            hands_latent = (
                latent_dict["rec_hands"] if cfg.lh > 0 and cfg.ch == 0 else None
            )
            lower_latent = (
                latent_dict["rec_lower"] if cfg.ll > 0 and cfg.cl == 0 else None
            )
            face_index = (
                torch.max(F.log_softmax(latent_dict["cls_face"], dim=2), dim=2)[1]
                if cfg.cf > 0
                else None
            )
            upper_index = (
                torch.max(F.log_softmax(latent_dict["cls_upper"], dim=2), dim=2)[1]
                if cfg.cu > 0
                else None
            )
            hands_index = (
                torch.max(F.log_softmax(latent_dict["cls_hands"], dim=2), dim=2)[1]
                if cfg.ch > 0
                else None
            )
            lower_index = (
                torch.max(F.log_softmax(latent_dict["cls_lower"], dim=2), dim=2)[1]
                if cfg.cl > 0
                else None
            )

            all_pred = self.motion_vq.decode(
                face_latent=face_latent,
                upper_latent=upper_latent,
                lower_latent=lower_latent,
                hands_latent=hands_latent,
                face_index=face_index,
                upper_index=upper_index,
                lower_index=lower_index,
                hands_index=hands_index,
                get_global_motion=True,
                ref_trans=ref_trans[:, 0],
            )

        t = all_pred["motion_axis_angle"].shape[1]
        return Motion(
            poses=all_pred["motion_axis_angle"].cpu().numpy().reshape(t, -1),
            expressions=all_pred["expression"].cpu().numpy().reshape(t, -1),
            trans=all_pred["trans"].cpu().numpy().reshape(t, -1),
        )


class MOV(ModuleWithHandle):
    """MOV Module

    Consumes streaming Audio chunks produced by TTS and generates whole-body
    SMPL-X motion using the EMAGE audio-to-gesture model.

    Sliding window
    ──────────────
    TTS emits short, uneven audio chunks. Running EMAGE on each chunk in
    isolation produces motion that is jerky at chunk seams (the model has no
    context across boundaries) and is slow because the per-chunk overhead is
    re-paid for tiny inputs — and gets worse the longer the utterance runs if
    naively re-fed the whole buffer.

    Instead we keep a rolling buffer and, each time at least ``min_chunk_sec``
    of fresh audio has arrived, run inference over a window of
    ``[context_sec of already-spoken audio] + [the fresh audio]``. The context
    primes the model so the seam is continuous; only the motion frames for the
    fresh audio are emitted. The window length is bounded by
    ``context_sec + chunk size`` so inference cost stays flat regardless of
    utterance length.

    Seam blending
    ─────────────
    Priming with context keeps the *audio* continuous across a window, but the
    motion still snaps at seams: EMAGE has no future context at a window's right
    edge, so its last frames wind down differently from how the next window —
    fully primed — opens, and at utterance boundaries it cold-starts from a rest
    pose entirely. So each fresh segment is eased onto the previously emitted
    frame: poses, expressions and root translation all start exactly continuous
    and a cosine-decaying offset fades to zero over ``blend_sec``, restoring the
    model's intended motion (and avoiding the cumulative drift a constant rebase
    would cause). The anchors survive the end-of-utterance reset, so the first
    window of the next utterance blends out of the pose still on screen instead
    of teleporting.

    input:  audio (Audio)
    output: motion (Motion)

    :hf_repo:      HuggingFace repository to load EMAGE weights from.
    :device:       PyTorch device string; defaults to CUDA when available.
    :context_sec:  Seconds of prior audio prepended to each window for continuity.
    :min_chunk_sec: Minimum seconds of fresh audio to accumulate before inferring.
    :blend_sec:    Seconds over which each window's seam is eased onto the prior frame.
    """

    _handle_cls = MOVHandle
    input_type = "audio.out"
    output_type = "motion"

    def __init__(
        self,
        _handle: handle.DeploymentHandle,
        context_sec: float = _CONTEXT_SEC,
        min_chunk_sec: float = _MIN_CHUNK_SEC,
        blend_sec: float = _BLEND_SEC,
    ):
        super().__init__(_handle)
        self._context_sec = float(context_sec)
        self._min_chunk_sec = float(min_chunk_sec)
        self._blend_sec = float(blend_sec)

        # Per-utterance sliding-window state. All sample counts are in the
        # source sample rate; resampling to 16 kHz happens once inside infer().
        self._lock = asyncio.Lock()
        self._sr: Optional[int] = None
        self._buffer = np.empty(
            0, dtype=np.float32
        )  # trailing audio (ctx + unprocessed)
        self._buf_start = 0  # source-sr sample index of buffer[0] in utterance timeline
        self._emitted = 0  # source-sr samples whose motion has been emitted

        # Last emitted frame per channel, used to ease the next segment's seam.
        # These persist across the end-of-utterance reset (see _end_utterance) so
        # gestures stay continuous when a new utterance starts.
        self._trans_anchor: Optional[np.ndarray] = None
        self._pose_anchor: Optional[np.ndarray] = None
        self._expr_anchor: Optional[np.ndarray] = None

    def _end_utterance(self) -> None:
        # Reset only per-utterance buffering/timeline state. The seam anchors
        # deliberately survive so the first window of the next utterance eases
        # out of the pose currently on screen instead of snapping to EMAGE's
        # cold-start rest pose.
        self._sr = None
        self._buffer = np.empty(0, dtype=np.float32)
        self._buf_start = 0
        self._emitted = 0

    async def process(self, audio: Audio) -> AsyncGenerator[Motion, None]:
        # Each chunk arrives as its own process() task on the shared per-session
        # instance, so serialise under a lock to keep the buffer ordered.
        async with self._lock:
            if audio.data.size > 0:
                if self._sr is None:
                    self._sr = audio.sample_rate
                self._buffer = np.concatenate(
                    [self._buffer, audio.data.astype(np.float32)]
                )

            sr = self._sr
            end_of_utterance = audio.end

            if sr is None:
                # Nothing buffered yet (e.g. a lone end marker). Reset and bail.
                if end_of_utterance:
                    self._end_utterance()
                return

            ctx_samples = int(self._context_sec * sr)
            min_new_samples = int(self._min_chunk_sec * sr)

            global_end = self._buf_start + len(self._buffer)
            new_samples = global_end - self._emitted

            # Wait for more audio unless this is the final flush of the utterance.
            if new_samples <= 0 or (
                not end_of_utterance and new_samples < min_new_samples
            ):
                if end_of_utterance:
                    self._end_utterance()
                return

            motion = await self._infer_window(sr, ctx_samples, global_end)
            if motion is not None:
                yield motion

            if end_of_utterance:
                self._end_utterance()

    async def _infer_window(
        self, sr: int, ctx_samples: int, global_end: int
    ) -> Optional[Motion]:
        # Window = [context of already-emitted audio] + [fresh audio].
        win_start = max(self._buf_start, self._emitted - ctx_samples)
        window = self._buffer[win_start - self._buf_start :]
        if window.size == 0:
            return None

        motion: Motion = await self._handle.infer.remote(window, sr)
        total_frames = motion.poses.shape[0]

        # EMAGE's internal windowing (EmageAudioModel.inference) emits a
        # contiguous *prefix* of the requested window and silently drops up to
        # ~2*seed_frames frames off the END whenever the trailing partial window
        # is shorter than its motion seed. So the returned frames cover only
        # [win_start, win_start + total_frames] — not necessarily the whole
        # window. Map emission off the actual frame count, not the requested
        # length: otherwise the freshest motion is dropped while _emitted skips
        # over it, tearing a hole in the timeline that reads as a freeze-then-
        # jump (and drifts gesture out of sync with speech).
        covered_end = win_start + int(round(total_frames * sr / motion.fps))

        # Drop the leading frames that correspond to the context (already emitted).
        skip_sec = (self._emitted - win_start) / sr
        skip_frames = int(round(skip_sec * motion.fps))
        skip_frames = max(0, min(skip_frames, total_frames))

        poses = motion.poses[skip_frames:].copy()
        expressions = motion.expressions[skip_frames:].copy()
        trans = motion.trans[skip_frames:].copy()

        # Advance only past audio the model actually turned into motion; any
        # dropped tail stays buffered and is re-inferred next window, this time
        # with real right-context. Cap at global_end so rounding can't overrun
        # the buffer, and never move backwards.
        self._emitted = min(global_end, max(self._emitted, covered_end))
        self._trim_buffer(ctx_samples)

        if poses.shape[0] == 0:
            return None

        # Ease this segment's seam onto the last emitted frame. Poses and
        # expressions snap because EMAGE regenerates the boundary without the
        # right-context the next window will have (and cold-starts across
        # utterances); root translation snaps because every window restarts near
        # the origin. Blending all three keeps the seam continuous, and the
        # decaying (vs. constant) offset returns to the model's intended motion
        # so root translation doesn't accumulate drift across windows.
        blend_frames = int(round(self._blend_sec * motion.fps))
        self._blend_into(poses, self._pose_anchor, blend_frames)
        self._blend_into(expressions, self._expr_anchor, blend_frames)
        self._blend_into(trans, self._trans_anchor, blend_frames)

        self._pose_anchor = poses[-1].copy()
        self._expr_anchor = expressions[-1].copy()
        self._trans_anchor = trans[-1].copy()

        out = Motion(
            poses=poses,
            expressions=expressions,
            trans=trans,
            fps=motion.fps,
            pts=win_start / sr + skip_sec,  # == self._emitted_before / sr
        )
        return out

    @staticmethod
    def _blend_into(
        arr: np.ndarray, anchor: Optional[np.ndarray], blend_frames: int
    ) -> None:
        """Ease the start of a fresh segment onto ``anchor`` in place.

        Frame 0 is shifted to equal ``anchor`` (a continuous seam) and the
        offset fades to zero over ``blend_frames`` with a cosine ease — zero
        slope at both ends, so neither the value nor its velocity jumps — after
        which the segment is the model's untouched output.

        Poses are SMPL-X axis-angle, so this is a linear blend in axis-angle
        space: exact only for small seam offsets, which is the regime here since
        consecutive frames are already close. A quaternion slerp would be needed
        for large discontinuities but is overkill for seam clean-up.
        """
        if anchor is None or blend_frames <= 0 or arr.shape[0] == 0:
            return
        n = min(blend_frames, arr.shape[0])
        w = 0.5 * (1.0 + np.cos(np.pi * np.linspace(0.0, 1.0, n, dtype=arr.dtype)))
        arr[:n] += w[:, None] * (anchor - arr[0])

    def _trim_buffer(self, ctx_samples: int) -> None:
        # Keep one context window of audio before the last *emitted* sample so
        # the next window stays bounded — but never discard audio whose motion
        # hasn't been emitted yet (the dropped tail above lives between
        # _emitted and the buffer end).
        keep_from = self._emitted - ctx_samples
        if keep_from > self._buf_start:
            self._buffer = self._buffer[keep_from - self._buf_start :]
            self._buf_start = keep_from
