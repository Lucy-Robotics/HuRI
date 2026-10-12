import asyncio
import logging

import numpy as np
from huri_events.question import RAGQuestion
from huri_events.speech import Transcript

from huri_core.core.client import HuRIClient
from huri_core.core.events import BytesEvent

logger = logging.getLogger("huri_cli")


async def send_microphone(
    client: HuRIClient,
    topic: str,
    sample_rate: int = 16000,
    frame_duration: float = 0.030,
) -> None:
    """Streams 16-bit mono microphone frames until cancelled."""

    import sounddevice as sd

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

    def callback(indata: np.ndarray, frames, time, status):
        if status:
            logger.warning("microphone: %s", status)
        loop.call_soon_threadsafe(queue.put_nowait, indata.copy())

    logger.info("microphone: %s", sd.query_devices(kind="input")["name"])
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
    """Sends each typed line as a question; returns on `\\exit` or EOF."""

    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    print("'\\exit' or CTRL+D/C to exit.")
    session: PromptSession = PromptSession()
    try:
        while True:
            with patch_stdout():
                text = await session.prompt_async(">> ")
            if text == "\\exit":
                return
            if text.strip():
                await client.send(topic, RAGQuestion(Transcript(text, True), None))
    except (EOFError, KeyboardInterrupt):
        pass
