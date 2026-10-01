import asyncio
import os
import wave
from datetime import datetime
from typing import Dict, List, Optional, Type

import numpy as np
import sounddevice as sd
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from scipy.signal import resample

from src.core.client import ClientHook, ClientSender
from src.core.events import BytesEvent
from src.core.interface import Interface
from src.modules.gesture.events import Motion
from src.modules.rag.events import RAGQuestion
from src.modules.speech_to_text.events import Transcript
from src.modules.text_to_speech.events import Audio, Token


class AudioSender(ClientSender[BytesEvent]):
    def __init__(
        self, sample_rate: int = 16000, frame_duration: float = 0.030, **kwargs
    ):
        super().__init__(**kwargs)

        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_duration)

    async def input_loop(self, ws):
        loop = asyncio.get_running_loop()

        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def callback(indata: np.ndarray, frames, time, status):
            loop.call_soon_threadsafe(queue.put_nowait, indata.copy())

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            callback=callback,
            blocksize=self.frame_size,
        ):
            while True:
                chunk = await queue.get()
                await self.send(ws, BytesEvent(data=chunk.tobytes()))


class TextSender(ClientSender[RAGQuestion]):
    output_type = RAGQuestion

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def input_loop(self, ws):
        print("'\\exit' or CTRL+D/C to exit.")
        session: PromptSession = PromptSession()
        try:
            while True:
                with patch_stdout():
                    text = await session.prompt_async(">> ")
                if text == "\\exit":
                    return
                await self.send(ws, RAGQuestion(Transcript(text, True), None))

        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            print("TextSender Exited...")


class AudioHook(ClientHook[Audio]):
    input_type = Audio

    def __init__(
        self,
        # Defaulted, because the body already treats it as optional ("" = do
        # not save). Hooks are constructed as available_hooks[name](**hook.args)
        # from the yaml, so a required parameter here is a hard TypeError at
        # client startup for every config that omits the key — which was all of
        # them except client_text.yaml.
        save_audio_dir: str = "",
        sample_rate=48000,
        incoming_sample_rate=16000,
        **kwargs,
    ):
        super().__init__(**kwargs)

        print("Speaker:", sd.query_devices(kind="output"))

        self.incoming_sample_rate = incoming_sample_rate
        self.sample_rate = sample_rate
        self.stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        self.stream.start()
        self._stream_sr = sample_rate
        self._resample_fallback = False

        self.save_audio_dir = save_audio_dir
        self._audio_buf: List[np.ndarray] = []
        self._audio_sr: Optional[int] = None
        self._audio_idx = 0
        if save_audio_dir:
            os.makedirs(save_audio_dir, exist_ok=True)

    def _resample(self, audio: np.ndarray) -> np.ndarray:
        """
        Resample float32 audio to the output device's rate.
        """
        return np.asarray(
            resample(
                audio,
                int(len(audio) * self.sample_rate / self.incoming_sample_rate),
            ),
            dtype=np.float32,
        )

    def _ensure_stream_for(self, sample_rate: int) -> None:
        """
        Run the output device at the TTS rate when it can.
        """
        if sample_rate == self._stream_sr or self._resample_fallback:
            return
        try:
            stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
            stream.start()
        except Exception as e:  # noqa: BLE001 - device capability probe
            print(
                f"** output device will not run at {sample_rate}Hz ({e}); "
                f"resampling to {self.sample_rate}Hz instead (expect chunk clicks)"
            )
            self._resample_fallback = True
            return
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.stream = stream
        self._stream_sr = sample_rate
        print(f"** audio output reopened at {sample_rate}Hz")

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
        print(
            f"** saved audio: {path} ({audio.size} samples, "
            f"~{audio.size / self._audio_sr:.2f}s @ {self._audio_sr}Hz)"
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

    async def hook(self, data: Audio):
        print(
            f"<< audio: pts={data.pts:.3f}s "
            f"samples={data.data.size} @ {data.sample_rate}Hz "
            f"end={bool(data.end)}"
        )

        samples = np.asarray(data.data, dtype=np.float32)
        if samples.size:

            self.incoming_sample_rate = data.sample_rate
            self._ensure_stream_for(data.sample_rate)
            if self._stream_sr != data.sample_rate:
                samples = self._resample(samples)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            try:
                self.stream.write(pcm.reshape(-1, 1))
            except Exception as e:  # noqa: BLE001 - never kill the session on audio
                print(f"** audio playback failed: {type(e).__name__}: {e}")

        if self.save_audio_dir:
            self._collect_audio(data.data, data.sample_rate, bool(data.end))


class TextHook(ClientHook[RAGQuestion]):
    input_type = RAGQuestion

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def hook(self, data: RAGQuestion):
        print("<<", data.transcript, data.emotion)


class TokenHook(ClientHook[Token]):
    """Print the RAG's streamed reply token-by-token (topic ``token``).

    This is the live text output of the pipeline (RAG -> Token). The website
    backend consumes the same stream; the CLI just echoes each delta inline and
    emits a newline on the end-of-utterance marker.
    """

    input_type = Token

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def hook(self, data: Token):
        if data.end:
            print()
        else:
            print(data.text, end="", flush=True)


class MotionHook(ClientHook[Motion]):
    input_type = Motion

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def hook(self, data: Motion):
        print(
            f"<< motion: pts={data.pts:.3f}s "
            f"frames={data.poses.shape[0]} @ {data.fps}fps"
        )


class CLIInterface(Interface):
    def __init__(self):
        super().__init__(singletton=None)

    def get_senders(self) -> Dict[str, Type[ClientSender]]:
        return {"audio": AudioSender, "text": TextSender}

    def get_hooks(self) -> Dict[str, Type[ClientHook]]:
        return {
            "audio": AudioHook,
            "text": TextHook,
            "token": TokenHook,
            "motion": MotionHook,
        }


cli_interface = CLIInterface()
