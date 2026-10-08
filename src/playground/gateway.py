"""Public demo gateway: visitors talk to HuRI, then answer a questionnaire.

The browser never reaches HuRI directly. HuRI builds whatever pipeline the
first message of a session describes and has no notion of a visitor, so this
gateway sits in between and owns everything a public page needs:

  * the pipeline: the browser picks a preset id, the modules stay server-side.
    A preset's optional modules (mov) are dropped when HuRI cannot run them;
  * the cost: a cap on concurrent sessions and on session length;
  * abuse: an optional shared access code, a topic allow-list, per-topic rate
    and size limits;
  * the questionnaire: a session id handed out at ``ready`` is the one-time
    ticket to submit answers, so every response maps to a real conversation.

Browser <-> gateway protocol (JSON text frames unless noted):

  -> {"type": "start", "preset": "text", "access_code": "..."}
  <- {"type": "ready", "session_id", "max_seconds", "inbound", "outbound"}
     | {"type": "error", "reason"}   then close
  -> {"topic": "question", "data": {"text": "..."}}   relayed to HuRI
  -> binary [u16 topic_len][topic][payload]           relayed to HuRI
  <- HuRI events, text or binary, relayed unchanged
  <- {"type": "rejected", "reason", "topic"}           frame dropped, keep going
  -> {"type": "finish"}
  <- {"type": "ended", "reason"}   then close
"""

import asyncio
import hmac
import json
import logging
import os
import struct
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple

import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from .limits import RateLimiter, SessionSlots
from .presets import Preset, load_presets
from .questionnaire import InvalidAnswers, Questionnaire, load_questionnaires
from .store import AlreadyAnswered, ResponseStore, SessionRecord, UnknownSession

logger = logging.getLogger("playground")

STATIC_DIR = Path(__file__).parent / "static"

START_TIMEOUT_S = 30.0
# Session creation instantiates every module of the preset on the HuRI side.
HURI_INIT_TIMEOUT_S = 60.0
MAX_COMMENT_CHARS = 2000


@dataclass
class Settings:
    huri_url: str = "ws://localhost:8000/session"
    access_code: str = ""
    max_sessions: int = 2
    session_seconds: float = 300
    max_frame_bytes: int = 64 * 1024
    data_dir: Path = Path("playground_data")
    questionnaires: List[str] = field(default_factory=lambda: ["godspeed", "rosas"])

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        default = cls()
        return cls(
            huri_url=env.get("HURI_URL", default.huri_url),
            access_code=env.get("PLAYGROUND_ACCESS_CODE", default.access_code),
            max_sessions=int(env.get("PLAYGROUND_MAX_SESSIONS", default.max_sessions)),
            session_seconds=float(
                env.get("PLAYGROUND_SESSION_SECONDS", default.session_seconds)
            ),
            data_dir=Path(env.get("PLAYGROUND_DATA_DIR", default.data_dir)),
            questionnaires=[
                q.strip()
                for q in env.get(
                    "PLAYGROUND_QUESTIONNAIRES", ",".join(default.questionnaires)
                ).split(",")
                if q.strip()
            ],
        )


class Gateway:
    def __init__(
        self,
        settings: Settings,
        presets: Mapping[str, Preset],
        questionnaires: List[Questionnaire],
        store: ResponseStore,
        connect: Callable[..., Any] = websockets.connect,
    ):
        self.settings = settings
        self.presets = presets
        self.questionnaires = questionnaires
        self.store = store
        self.slots = SessionSlots(settings.max_sessions)
        self.connect = connect

    # --- HTTP -------------------------------------------------------------

    def public_config(self) -> Dict[str, Any]:
        return {
            "access_code_required": bool(self.settings.access_code),
            "max_seconds": self.settings.session_seconds,
            "presets": [p.public() for p in self.presets.values()],
            "questionnaires": [q.definition for q in self.questionnaires],
        }

    def submit(self, payload: Any) -> JSONResponse:
        if not isinstance(payload, Mapping):
            return _error(400, "body must be a JSON object")
        session_id = payload.get("session_id")
        answers = payload.get("answers")
        comment = payload.get("comment") or ""
        if not isinstance(session_id, str) or not isinstance(answers, Mapping):
            return _error(400, "session_id and answers are required")
        if not isinstance(comment, str) or len(comment) > MAX_COMMENT_CHARS:
            return _error(400, f"comment must be text under {MAX_COMMENT_CHARS}")

        expected = {q.id for q in self.questionnaires}
        if set(answers) != expected:
            return _error(400, f"answers must cover exactly {sorted(expected)}")
        try:
            clean = {q.id: q.validate(answers[q.id]) for q in self.questionnaires}
        except InvalidAnswers as e:
            return _error(400, str(e))
        scores = {q.id: q.score(clean[q.id]) for q in self.questionnaires if q.scores}

        try:
            self.store.submit(session_id, clean, comment.strip(), scores)
        except UnknownSession:
            return _error(404, "unknown or expired session")
        except AlreadyAnswered:
            return _error(409, "this session was already answered")
        return JSONResponse({"ok": True}, status_code=201)

    # --- WebSocket session ------------------------------------------------

    async def run_session(self, ws: WebSocket) -> None:
        await ws.accept()
        try:
            start = await asyncio.wait_for(ws.receive_json(), START_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - timeout, bad JSON or early disconnect
            await _close(ws)
            return

        if not isinstance(start, dict) or start.get("type") != "start":
            await _fail(ws, "expected_start")
            return
        if not self._access_ok(start.get("access_code")):
            await _fail(ws, "bad_access_code")
            return
        preset = self.presets.get(start.get("preset", ""))
        if preset is None:
            await _fail(ws, "unknown_preset")
            return
        if not self.slots.try_acquire():
            await _fail(ws, "busy")
            return

        try:
            await self._run_preset(ws, preset)
        finally:
            self.slots.release()

    def _access_ok(self, given: Any) -> bool:
        expected = self.settings.access_code
        if not expected:
            return True
        return isinstance(given, str) and hmac.compare_digest(
            given.encode(), expected.encode()
        )

    async def _run_preset(self, ws: WebSocket, preset: Preset) -> None:
        session_id = uuid.uuid4().hex
        user_id = f"playground-{session_id}"

        async with AsyncExitStack() as stack:
            try:
                huri, reply = await self._open(stack, preset, user_id)
                missing = _missing_optional(preset, reply)
                if missing:
                    logger.info(
                        "playground: HuRI has no %s, %s runs without it",
                        ", ".join(sorted(missing)),
                        preset.id,
                    )
                    await stack.aclose()  # HuRI closes a rejected session anyway
                    preset = preset.without(missing)
                    huri, reply = await self._open(stack, preset, user_id)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                logger.warning("playground: HuRI unreachable: %r", e)
                await _fail(ws, "huri_unavailable")
                return

            if reply.get("type") != "session_init":
                logger.error(
                    "playground: HuRI rejected preset %s: %s", preset.id, reply
                )
                await _fail(ws, "huri_rejected")
                return

            record = self.store.open_session(session_id, preset.id, preset.modules)
            logger.info("playground: session %s started (%s)", session_id, preset.id)
            await ws.send_json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "max_seconds": self.settings.session_seconds,
                    "inbound": {
                        topic: {"max_chars": rule.max_chars}
                        for topic, rule in preset.inbound.items()
                    },
                    # What HuRI will stream: with "motion", the avatar gestures.
                    "outbound": preset.outbound,
                }
            )

            reason = await self._relay(ws, huri, preset, record)
            self.store.close_session(session_id, reason)
            logger.info("playground: session %s ended (%s)", session_id, reason)

        if reason != "browser_left":
            await _send(ws, {"type": "ended", "reason": reason})
            await _close(ws)

    async def _open(
        self, stack: AsyncExitStack, preset: Preset, user_id: str
    ) -> Tuple[Any, Dict[str, Any]]:
        """Connect to HuRI and send the preset's handshake; returns HuRI's reply."""
        huri = await stack.enter_async_context(self.connect(self.settings.huri_url))
        await huri.send(json.dumps(preset.handshake(user_id, self.settings.huri_url)))
        reply = json.loads(await asyncio.wait_for(huri.recv(), HURI_INIT_TIMEOUT_S))
        return huri, reply

    async def _relay(
        self, ws: WebSocket, huri: Any, preset: Preset, record: SessionRecord
    ) -> str:
        """Pump frames both ways until one side stops or time runs out."""
        browser = asyncio.create_task(self._from_browser(ws, huri, preset, record))
        server = asyncio.create_task(_from_huri(ws, huri))

        done, pending = await asyncio.wait(
            {browser, server},
            timeout=self.settings.session_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if not done:
            return "time_limit"
        task = done.pop()
        if task.exception() is not None:
            logger.error("playground: relay failed", exc_info=task.exception())
            return "error"
        return str(task.result())

    async def _from_browser(
        self, ws: WebSocket, huri: Any, preset: Preset, record: SessionRecord
    ) -> str:
        limiters = {
            topic: RateLimiter(rule.max_per_minute)
            for topic, rule in preset.inbound.items()
        }

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "browser_left"

            if msg.get("bytes") is not None:
                frame: bytes = msg["bytes"]
                topic = _binary_topic(frame)
                limiter = limiters.get(topic or "")
                if len(frame) > self.settings.max_frame_bytes:
                    await _reject(ws, "too_large", topic)
                elif limiter is None:
                    await _reject(ws, "topic_not_allowed", topic)
                elif not limiter.allow():
                    await _reject(ws, "rate_limited", topic)
                else:
                    await huri.send(frame)
                continue

            try:
                event = json.loads(msg.get("text") or "")
            except ValueError:
                await _reject(ws, "bad_json", None)
                continue
            if not isinstance(event, dict):
                await _reject(ws, "bad_json", None)
                continue
            if event.get("type") == "finish":
                return "finished"

            topic = event.get("topic")
            data = event.get("data")
            key = topic if isinstance(topic, str) else ""
            rule = preset.inbound.get(key)
            if rule is None:
                await _reject(ws, "topic_not_allowed", topic)
            elif not isinstance(data, dict) or not _text_ok(data, rule.max_chars):
                await _reject(ws, "bad_data", topic)
            elif not limiters[key].allow():
                await _reject(ws, "rate_limited", topic)
            else:
                await huri.send(json.dumps({"topic": topic, "data": data}))
                if topic == "question":
                    record.questions += 1


def _missing_optional(preset: Preset, reply: Mapping[str, Any]) -> set[str]:
    """Optional modules to drop after HuRI refused the session for lacking them.

    HuRI's ``session_error`` lists the modules it can build; without that list
    the cause is unknown and nothing is retried.
    """
    if reply.get("type") != "session_error" or "server_modules" not in reply:
        return set()
    return preset.missing_optional(reply["server_modules"])


async def _from_huri(ws: WebSocket, huri: Any) -> str:
    try:
        async for msg in huri:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)
    except websockets.ConnectionClosed:
        pass
    return "huri_closed"


def _binary_topic(frame: bytes) -> str | None:
    if len(frame) < 2:
        return None
    (topic_len,) = struct.unpack(">H", frame[:2])
    try:
        return frame[2 : 2 + topic_len].decode()
    except UnicodeDecodeError:
        return None


def _text_ok(data: Mapping[str, Any], max_chars: int | None) -> bool:
    if max_chars is None:
        return True
    text = data.get("text")
    return isinstance(text, str) and 0 < len(text.strip()) and len(text) <= max_chars


async def _send(ws: WebSocket, message: Dict[str, Any]) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_json(message)
    except Exception:  # noqa: BLE001 - the browser may be gone already
        pass


async def _close(ws: WebSocket) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass


async def _fail(ws: WebSocket, reason: str) -> None:
    await _send(ws, {"type": "error", "reason": reason})
    await _close(ws)


async def _reject(ws: WebSocket, reason: str, topic: Any) -> None:
    await _send(ws, {"type": "rejected", "reason": reason, "topic": topic})


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": detail}, status_code=status)


def create_app(
    settings: Settings | None = None,
    connect: Callable[..., Any] = websockets.connect,
) -> FastAPI:
    settings = settings or Settings.from_env()
    gateway = Gateway(
        settings=settings,
        presets=load_presets(),
        questionnaires=load_questionnaires(settings.questionnaires),
        store=ResponseStore(settings.data_dir),
        connect=connect,
    )

    app = FastAPI(title="HuRI playground", docs_url=None, redoc_url=None)
    app.state.gateway = gateway
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_page(request: Request, call_next: Any) -> Any:
        # The page and its assets change with every deploy; without this a
        # returning visitor's browser may pair a fresh page with stale JS/CSS.
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config() -> Dict[str, Any]:
        return gateway.public_config()

    @app.post("/api/responses")
    async def responses(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return _error(400, "body must be JSON")
        return gateway.submit(payload)

    @app.websocket("/ws")
    async def session(ws: WebSocket) -> None:
        await gateway.run_session(ws)

    return app
