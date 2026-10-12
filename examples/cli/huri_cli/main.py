"""huri-cli: talk to a HuRI server from a terminal.

    huri-cli --config config/client_full.yaml [--log-level DEBUG] [--log-file cli.log]

The client config's `send:` and `subscribe:` pick among the topics below; its
`cli:` section holds the device settings:

    cli:
      audio_in:  {sample_rate: 16000, frame_duration: 0.030}
      audio_out: {sample_rate: 44100, save_audio_dir: ""}
      motion:    {save_motion_dir: ""}
"""

import argparse
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple, Type

from huri_events.audio import Audio
from huri_events.motion import Motion
from huri_events.question import RAGQuestion
from huri_events.text import Token

from huri_core.core.client import (
    HuRIClient,
    HuRISessionError,
    load_raw_client_config,
)
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.events import EventData

from .audio import AudioPlayer
from .senders import send_microphone, send_prompt
from .viewer import MotionViewer

logger = logging.getLogger("huri_cli")


async def print_question(data: RAGQuestion) -> None:
    print(
        "<<", data.transcript.text, f"(emotion: {data.emotion})" if data.emotion else ""
    )


async def print_token(data: Token) -> None:
    """The RAG's streamed reply, token by token; newline at end of utterance."""
    if data.end:
        print()
    else:
        print(data.text, end="", flush=True)


Receiver = Callable[[Any], Awaitable[None]]

# topic -> (event class, factory building the handler from the `cli:` settings)
RECEIVERS: Dict[str, Tuple[Type[EventData], Callable[[Mapping], Receiver]]] = {
    "huri.audio.out": (Audio, lambda cli: AudioPlayer(**cli.get("audio_out", {}))),
    "huri.motion": (Motion, lambda cli: MotionViewer(**cli.get("motion", {}))),
    "huri.question": (RAGQuestion, lambda cli: print_question),
    "huri.token": (Token, lambda cli: print_token),
}

# topic -> coroutine sending it until it returns or is cancelled
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
            f"huri-cli cannot handle topic(s) {unsupported}. It sends "
            f"{sorted(SENDERS)} and receives {sorted(RECEIVERS)}"
        )
    client = HuRIClient.from_config(config)
    for topic in config.subscribe:
        event_cls, make = RECEIVERS[topic]
        client.on(topic, event_cls, make(cli))
    return client


async def run(config: ClientConfig, cli: Optional[Mapping] = None) -> int:
    cli = cli or {}
    client = build_client(config, cli)
    try:
        await client.connect()
    except HuRISessionError as e:
        logger.error("HuRI rejected this client config: %s", e.error)
        if e.server_modules:
            logger.error("  server registered : %s", ", ".join(e.server_modules))
            logger.error("  this config wants : %s", ", ".join(config.modules))
        return 1
    except OSError as e:
        logger.error(
            "cannot reach HuRI at %s: %s. Is the server deployed? "
            "(.huri-local/status.sh; replica logs in "
            "/tmp/ray/session_latest/logs/serve/)",
            config.huri_url,
            e,
        )
        return 1
    logger.info("session started, user_id=%s", client.user_id)
    try:
        loops: List[asyncio.Future] = [
            asyncio.ensure_future(SENDERS[topic](client, topic, cli))
            for topic in config.send
        ]
        closed = asyncio.ensure_future(client.wait_closed())
        # The session ends when a sender returns (`\exit` at the prompt) or
        # the server closes the connection.
        done, pending = await asyncio.wait(
            loops + [closed], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            if task is not closed and task.exception() is not None:
                logger.error("sender failed: %r", task.exception())
                return 1
        if closed in done:
            logger.info("the server closed the session")
        return 0
    finally:
        await client.close()


def setup_logging(level: str, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="huri-cli", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--config", required=True, help="client config file (YAML)")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING...")
    parser.add_argument("--log-file", help="also write the logs to this file")
    args = parser.parse_args(argv)

    setup_logging(args.log_level, args.log_file)
    raw = load_raw_client_config(args.config)
    config = ClientConfig.from_dict(raw)
    try:
        code = asyncio.run(run(config, dict(raw.get("cli") or {})))
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)
