import asyncio
import logging
import os
import wave
from datetime import datetime
from typing import Any, Callable, List, Optional

import numpy as np
from scipy.signal import resample

from huri_core.modules.text_to_speech.events import Audio

logger = logging.getLogger("huri_cli.audio")


def _open_output(sample_rate: int) -> Any:
    import sounddevice as sd

    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
    stream.start()
    return stream


class AudioPlayer:
    """Plays `huri.audio.out` on the default output device, optionally saving
    each utterance as a WAV file.

    The SDK hands chunks over in order, one at a time; the blocking device
    write runs in a worker thread so the event loop keeps receiving meanwhile.
    The device is opened on the first chunk, at the TTS sample rate when the
    device supports it, else at `sample_rate` with resampling.
    """

    def __init__(
        self,
        save_audio_dir: str = "",
        sample_rate: int = 48000,
        incoming_sample_rate: int = 16000,
        open_output: Callable[[int], Any] = _open_output,
    ):
        self.sample_rate = sample_rate
        self.incoming_sample_rate = incoming_sample_rate
        self._open_output = open_output
        self.stream: Any = None
        self._stream_sr: Optional[int] = None
        self._resample_fallback = False

        self.save_audio_dir = save_audio_dir
        self._audio_buf: List[np.ndarray] = []
        self._audio_sr: Optional[int] = None
        self._audio_idx = 0
        if save_audio_dir:
            os.makedirs(save_audio_dir, exist_ok=True)

    def _resample(self, audio: np.ndarray) -> np.ndarray:
        """Resample float32 audio to the output device's rate."""
        return np.asarray(
            resample(
                audio,
                int(len(audio) * self.sample_rate / self.incoming_sample_rate),
            ),
            dtype=np.float32,
        )

    def _ensure_stream_for(self, sample_rate: int) -> None:
        """Run the output device at the TTS rate when it can."""
        if sample_rate == self._stream_sr:
            return
        if self._resample_fallback:
            if self.stream is None:
                self.stream = self._open_output(self.sample_rate)
                self._stream_sr = self.sample_rate
            return
        try:
            stream = self._open_output(sample_rate)
        except Exception as e:  # noqa: BLE001 - device capability probe
            logger.warning(
                "output device will not run at %sHz (%s); resampling to %sHz "
                "instead (expect chunk clicks)",
                sample_rate,
                e,
                self.sample_rate,
            )
            self._resample_fallback = True
            self._ensure_stream_for(sample_rate)
            return
        self.close()
        self.stream = stream
        self._stream_sr = sample_rate
        logger.info("audio output opened at %sHz", sample_rate)

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None

    def _collect_audio(self, samples: np.ndarray, sample_rate: int, end: bool) -> None:
        if samples.size:
            self._audio_buf.append(samples)
            self._audio_sr = sample_rate
        if end:
            self._flush_audio()

    def _flush_audio(self) -> None:
        if not self._audio_buf or self._audio_sr is None:
            self._audio_buf = []
            return
        audio = np.concatenate(self._audio_buf)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(
            self.save_audio_dir, f"utt-{self._audio_idx:03d}-{stamp}.wav"
        )
        self._write_wav(path, audio, self._audio_sr)
        logger.info(
            "saved audio: %s (%d samples, ~%.2fs @ %dHz)",
            path,
            audio.size,
            audio.size / self._audio_sr,
            self._audio_sr,
        )
        self._audio_idx += 1
        self._audio_buf = []

    @staticmethod
    def _write_wav(path: str, audio: np.ndarray, sample_rate: int) -> None:
        # float32 [-1, 1] -> 16-bit PCM, clipped to avoid wraparound on overshoot.
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.tobytes())

    async def __call__(self, data: Audio) -> None:
        logger.debug(
            "audio: pts=%.3fs samples=%d @ %dHz end=%s",
            data.pts,
            data.data.size,
            data.sample_rate,
            bool(data.end),
        )
        samples = np.asarray(data.data, dtype=np.float32)
        if samples.size:
            self.incoming_sample_rate = data.sample_rate
            try:
                self._ensure_stream_for(data.sample_rate)
                if self._stream_sr != data.sample_rate:
                    samples = self._resample(samples)
                pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
                await asyncio.to_thread(self.stream.write, pcm.reshape(-1, 1))
            except Exception as e:  # noqa: BLE001 - never kill the session on audio
                logger.error("audio playback failed: %s: %s", type(e).__name__, e)

        if self.save_audio_dir:
            # The incoming chunk, not the resampled one: saved at its own rate.
            self._collect_audio(data.data, data.sample_rate, bool(data.end))
