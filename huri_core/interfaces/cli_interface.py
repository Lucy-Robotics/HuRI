"""The reference command-line client: microphone and text prompt in, speech,
text and gesture logs out. Built on the client SDK (HuRIClient).

Topics it can send and receive are the keys of SENDERS and RECEIVERS; the
client config picks among them with `send:` and `subscribe:`, and its `cli:`
section holds the device settings.
"""

import asyncio
import os
import wave
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple, Type

import numpy as np
import sounddevice as sd
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from scipy.signal import resample

from huri_core.core.client import HuRIClient, HuRISessionError
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.events import BytesEvent, EventData
from huri_core.modules.gesture.events import Motion
from huri_core.modules.rag.events import RAGQuestion
from huri_core.modules.speech_to_text.events import Transcript
from huri_core.modules.text_to_speech.events import Audio, Token

# --- senders: coroutines feeding a topic until they return -------------------


async def send_microphone(
    client: HuRIClient,
    topic: str,
    sample_rate: int = 16000,
    frame_duration: float = 0.030,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

    def callback(indata: np.ndarray, frames, time, status):
        loop.call_soon_threadsafe(queue.put_nowait, indata.copy())

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        callback=callback,
        blocksize=int(sample_rate * frame_duration),
    ):
        while True:
            chunk = await queue.get()
            await client.send(topic, BytesEvent(data=chunk.tobytes()))


async def send_prompt(client: HuRIClient, topic: str) -> None:
    print("'\\exit' or CTRL+D/C to exit.")
    session: PromptSession = PromptSession()
    try:
        while True:
            with patch_stdout():
                text = await session.prompt_async(">> ")
            if text == "\\exit":
                return
            await client.send(topic, RAGQuestion(Transcript(text, True), None))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        print("Prompt exited...")


# --- receivers -----------------------------------------------------------------


class AudioPlayer:
    """Plays `huri.audio.out` on the default output device, optionally saving
    each utterance as a WAV file."""

    def __init__(
        self,
        save_audio_dir: str = "",
        sample_rate: int = 48000,
        incoming_sample_rate: int = 16000,
    ):

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

    async def __call__(self, data: Audio) -> None:
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


async def print_question(data: RAGQuestion) -> None:
    print("<<", data.transcript, data.emotion)


async def print_token(data: Token) -> None:
    """The RAG's streamed reply, token by token; newline at end of utterance."""
    if data.end:
        print()
    else:
        print(data.text, end="", flush=True)


async def print_motion(data: Motion) -> None:
    print(
        f"<< motion: pts={data.pts:.3f}s frames={data.poses.shape[0]} @ {data.fps}fps"
    )


Receiver = Callable[[Any], Awaitable[None]]

# topic -> (event class, factory building the handler from the `cli:` settings)
RECEIVERS: Dict[str, Tuple[Type[EventData], Callable[[Mapping], Receiver]]] = {
    "huri.audio.out": (Audio, lambda cli: AudioPlayer(**cli.get("audio_out", {}))),
    "huri.question": (RAGQuestion, lambda cli: print_question),
    "huri.token": (Token, lambda cli: print_token),
    "huri.motion": (Motion, lambda cli: print_motion),
}

# topic -> coroutine factory sending it
SENDERS: Dict[str, Callable[[HuRIClient, str, Mapping], Awaitable[None]]] = {
    "huri.audio.in": lambda client, topic, cli: send_microphone(
        client, topic, **cli.get("audio_in", {})
    ),
    "huri.question": lambda client, topic, cli: send_prompt(client, topic),
}


def build_client(config: ClientConfig, cli: Mapping) -> HuRIClient:
    unsupported = [t for t in config.send if t not in SENDERS]
    unsupported += [t for t in config.subscribe if t not in RECEIVERS]
    if unsupported:
        raise ValueError(
            f"the CLI cannot handle topic(s) {unsupported}. It sends "
            f"{sorted(SENDERS)} and receives {sorted(RECEIVERS)}"
        )
    client = HuRIClient(
        config.huri_url,
        modules={k: m.to_dict() for k, m in config.modules.items()},
        sends=config.send,
        user_id=config.user_id,
    )
    for topic in config.subscribe:
        event_cls, make = RECEIVERS[topic]
        client.on(topic, event_cls, make(cli))
    return client


async def run_cli(config: ClientConfig, cli: Optional[Mapping] = None) -> None:
    cli = cli or {}
    client = build_client(config, cli)
    try:
        await client.connect()
    except HuRISessionError as e:
        print(f"HuRI rejected this client config: {e.error}")
        if e.server_modules:
            print(f"  server registered : {', '.join(e.server_modules)}")
            print(f"  this config wants : {', '.join(config.modules)}")
        return
    except OSError as e:
        print(
            f"Cannot reach HuRI at {config.huri_url}: {e}\n"
            "  Check the server is deployed: .huri-local/status.sh\n"
            "  Replica errors: /tmp/ray/session_latest/logs/serve/"
        )
        return
    print(f"Session started with _user_id: {client.user_id}")
    try:
        loops: List[Awaitable[None]] = [
            SENDERS[topic](client, topic, cli) for topic in config.send
        ]
        if loops:
            # The session ends when a sender returns (e.g. `\\exit` at the prompt).
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(loop) for loop in loops],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        else:
            await client.wait_closed()
    finally:
        await client.close()
