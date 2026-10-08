"""The playground gateway, driven from a browser-side TestClient against a fake
HuRI. What matters here is what a public page must never get wrong: a visitor
cannot choose modules, reach topics outside the preset, or exceed the session
caps, and every stored answer maps to one real conversation.
"""

import asyncio
import json
import struct
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.playground.gateway import Settings, create_app
from src.playground.questionnaire import load_questionnaires


class FakeHuRI:
    """Answers each question with a short token stream, like RAG does.

    With ``server_modules``, a handshake asking for any other module is
    rejected the way HuRI does it: ``session_error`` listing what it has.
    """

    def __init__(self, init_reply=None, answer="Hello there", server_modules=None):
        self.init_reply = init_reply or {"type": "session_init", "user_id": "x"}
        self.answer = answer
        self.server_modules = server_modules
        self.sent = []
        self.handshakes = []
        self._out = None

    @property
    def out(self):
        # Created lazily: the gateway runs in TestClient's own event loop.
        if self._out is None:
            self._out = asyncio.Queue()
        return self._out

    @property
    def handshake(self):
        return self.handshakes[-1]

    def forwarded(self):
        return [m for m in self.sent if not _is_handshake(m)]

    async def send(self, msg):
        self.sent.append(msg)
        if _is_handshake(msg):
            self.handshakes.append(json.loads(msg))
            return
        if isinstance(msg, bytes) or self.answer is None:
            return
        for word in self.answer.split():
            token = {"topic": "token", "data": {"text": word + " ", "end": False}}
            self.out.put_nowait(json.dumps(token))
        end = {"topic": "token", "data": {"text": "", "end": True}}
        self.out.put_nowait(json.dumps(end))

    async def recv(self):
        wanted = {m["name"] for m in self.handshake["modules"].values()}
        if self.server_modules is not None and not wanted <= self.server_modules:
            return json.dumps(
                {
                    "type": "session_error",
                    "error": "ValueError: Unknown module",
                    "server_modules": sorted(self.server_modules),
                }
            )
        return json.dumps(self.init_reply)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.out.get()


def _is_handshake(msg):
    return isinstance(msg, str) and '"user_id"' in msg


def make_client(tmp_path, huri=None, connect=None, **overrides):
    huri = huri or FakeHuRI()

    @asynccontextmanager
    async def fake_connect(url):
        yield huri

    settings = Settings(data_dir=tmp_path, **overrides)
    app = create_app(settings, connect=connect or fake_connect)
    return TestClient(app), huri


def start(ws, preset="text", access_code=""):
    ws.send_json({"type": "start", "preset": preset, "access_code": access_code})
    return ws.receive_json()


def ask(ws, text):
    ws.send_json({"topic": "question", "data": {"text": text}})


def read_answer(ws):
    words = []
    while True:
        msg = ws.receive_json()
        assert msg["topic"] == "token", msg
        if msg["data"]["end"]:
            return "".join(words).strip()
        words.append(msg["data"]["text"])


def full_answers(value=3):
    return {
        q.id: {item_id: value for item_id in q.item_ids}
        for q in load_questionnaires(["godspeed", "rosas"])
    }


def stored(tmp_path):
    path = tmp_path / "responses.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_conversation_then_questionnaire(tmp_path):
    client, huri = make_client(tmp_path)

    with client.websocket_connect("/ws") as ws:
        ready = start(ws)
        assert ready["type"] == "ready"
        session_id = ready["session_id"]

        ask(ws, "What is your name?")
        assert read_answer(ws) == "Hello there"

        ws.send_json({"type": "finish"})
        assert ws.receive_json() == {"type": "ended", "reason": "finished"}

    res = client.post(
        "/api/responses",
        json={"session_id": session_id, "answers": full_answers(), "comment": " ok "},
    )
    assert res.status_code == 201

    [entry] = stored(tmp_path)
    assert entry["session_id"] == session_id
    assert entry["preset"] == "text"
    assert entry["questions_asked"] == 1
    assert entry["end_reason"] == "finished"
    assert entry["answers"] == full_answers()
    assert entry["scores"]["godspeed"]["likeability"] == 3.0
    assert set(entry["scores"]["rosas"]) == {"warmth", "competence", "discomfort"}
    assert entry["comment"] == "ok"
    # Ratings only: what the visitor said is not stored.
    assert "What is your name" not in json.dumps(entry)


def test_handshake_comes_from_the_server_side_preset(tmp_path):
    client, huri = make_client(tmp_path)

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "start",
                "preset": "text",
                "modules": {"tts": {"name": "tts"}},  # ignored
            }
        )
        assert ws.receive_json()["type"] == "ready"

    handshake = huri.handshake
    assert set(handshake["modules"]) == {"rag"}
    assert handshake["user_id"].startswith("playground-")
    assert [h["topics"] for h in handshake["hooks"].values()] == [["token"]]


def test_spoken_preset_streams_huri_audio_to_the_browser(tmp_path):
    client, huri = make_client(tmp_path)
    topic = b"audio.out"
    audio = (
        struct.pack(">H", len(topic))
        + topic
        + struct.pack(">IBd", 22050, 1, 0.0)
        + b"\x00" * 16
    )

    with client.websocket_connect("/ws") as ws:
        assert start(ws, preset="spoken")["type"] == "ready"
        huri.out.put_nowait(audio)
        assert ws.receive_bytes() == audio

    handshake = huri.handshake
    assert set(handshake["modules"]) == {"rag", "tts", "mov"}
    assert sorted(t for h in handshake["hooks"].values() for t in h["topics"]) == [
        "audio.out",
        "motion",
        "token",
    ]


def test_avatar_moves_when_huri_has_gestures(tmp_path):
    huri = FakeHuRI(server_modules={"rag", "tts", "mov"})
    client, _ = make_client(tmp_path, huri=huri)

    with client.websocket_connect("/ws") as ws:
        ready = start(ws, preset="spoken")
        assert ready["outbound"] == ["token", "audio.out", "motion"]
        session_id = ready["session_id"]

    assert len(huri.handshakes) == 1
    client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    assert stored(tmp_path)[0]["modules"] == ["mov", "rag", "tts"]


def test_without_gestures_huri_is_asked_again_without_mov(tmp_path):
    # A machine where EMAGE cannot run: same conversation, voice only.
    huri = FakeHuRI(server_modules={"rag", "tts"})
    client, _ = make_client(tmp_path, huri=huri)

    with client.websocket_connect("/ws") as ws:
        ready = start(ws, preset="spoken")
        assert ready["type"] == "ready"
        assert ready["outbound"] == ["token", "audio.out"]
        session_id = ready["session_id"]

        ask(ws, "Hello?")
        assert read_answer(ws) == "Hello there"

    first, second = huri.handshakes
    assert "mov" in first["modules"] and "mov" not in second["modules"]
    assert first["user_id"] == second["user_id"]

    # Which pipeline a visitor got is kept with the ratings: gestures change
    # how lifelike HuRI looks.
    client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    assert stored(tmp_path)[0]["modules"] == ["rag", "tts"]


def test_huri_missing_a_required_module_is_not_retried(tmp_path):
    huri = FakeHuRI(server_modules={"tts", "mov"})  # no rag
    client, _ = make_client(tmp_path, huri=huri)

    with client.websocket_connect("/ws") as ws:
        assert start(ws, preset="spoken") == {
            "type": "error",
            "reason": "huri_rejected",
        }
    assert len(huri.handshakes) == 1


def test_each_visit_gets_its_own_user_id(tmp_path):
    client, huri = make_client(tmp_path)
    ids = []
    for _ in range(2):
        with client.websocket_connect("/ws") as ws:
            start(ws)
        ids.append(json.loads(huri.sent[-1])["user_id"])
    assert ids[0] != ids[1]


def test_answers_can_only_be_submitted_once(tmp_path):
    client, _ = make_client(tmp_path)
    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]

    body = {"session_id": session_id, "answers": full_answers()}
    assert client.post("/api/responses", json=body).status_code == 201
    assert client.post("/api/responses", json=body).status_code == 409
    assert len(stored(tmp_path)) == 1


def test_answers_need_a_real_session(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.post(
        "/api/responses", json={"session_id": "made-up", "answers": full_answers()}
    )
    assert res.status_code == 404
    assert stored(tmp_path) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a["godspeed"].pop("kind"),
        lambda a: a.pop("rosas"),
        lambda a: a["rosas"].update(scary=8),
        lambda a: a["godspeed"].update(kind=0),
        lambda a: a["godspeed"].update(kind=True),
        lambda a: a["godspeed"].update(kind="3"),
        lambda a: a["rosas"].update(extra=3),
    ],
    ids=["missing", "no-rosas", "too-high", "too-low", "bool", "string", "unknown"],
)
def test_invalid_answers_are_refused(tmp_path, mutate):
    client, _ = make_client(tmp_path)
    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]

    answers = full_answers()
    mutate(answers)
    res = client.post(
        "/api/responses", json={"session_id": session_id, "answers": answers}
    )
    assert res.status_code == 400
    assert stored(tmp_path) == []


def test_access_code(tmp_path):
    client, _ = make_client(tmp_path, access_code="lucy")
    assert client.get("/api/config").json()["access_code_required"] is True

    with client.websocket_connect("/ws") as ws:
        assert start(ws, access_code="nope") == {
            "type": "error",
            "reason": "bad_access_code",
        }
    with client.websocket_connect("/ws") as ws:
        assert start(ws, access_code="lucy")["type"] == "ready"


def test_unknown_preset(tmp_path):
    client, huri = make_client(tmp_path)
    with client.websocket_connect("/ws") as ws:
        assert start(ws, preset="full")["reason"] == "unknown_preset"
    assert huri.sent == []


def test_busy_when_all_slots_are_taken(tmp_path):
    client, _ = make_client(tmp_path, max_sessions=1)

    with client.websocket_connect("/ws") as first:
        assert start(first)["type"] == "ready"
        with client.websocket_connect("/ws") as second:
            assert start(second) == {"type": "error", "reason": "busy"}
        first.send_json({"type": "finish"})
        assert first.receive_json()["reason"] == "finished"

    # The slot is free again once the first visitor is done.
    with client.websocket_connect("/ws") as third:
        assert start(third)["type"] == "ready"


def test_huri_unreachable(tmp_path):
    @asynccontextmanager
    async def refused(url):
        raise ConnectionRefusedError("no HuRI")
        yield

    client, _ = make_client(tmp_path, connect=refused)
    with client.websocket_connect("/ws") as ws:
        assert start(ws) == {"type": "error", "reason": "huri_unavailable"}

    # A failed start must not leak the session slot.
    assert client.app.state.gateway.slots.in_use == 0


def test_huri_rejects_the_session(tmp_path):
    huri = FakeHuRI(init_reply={"type": "session_error", "error": "no rag"})
    client, _ = make_client(tmp_path, huri=huri)
    with client.websocket_connect("/ws") as ws:
        assert start(ws) == {"type": "error", "reason": "huri_rejected"}


def test_topics_outside_the_preset_are_dropped(tmp_path):
    client, huri = make_client(tmp_path)

    with client.websocket_connect("/ws") as ws:
        start(ws)
        ws.send_json({"topic": "token", "data": {"text": "spoof", "end": False}})
        assert ws.receive_json() == {
            "type": "rejected",
            "reason": "topic_not_allowed",
            "topic": "token",
        }

        topic = b"audio.in"
        ws.send_bytes(struct.pack(">H", len(topic)) + topic + b"\x00" * 64)
        assert ws.receive_json()["reason"] == "topic_not_allowed"

    assert huri.forwarded() == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_empty_or_long_questions_are_dropped(tmp_path, text):
    client, huri = make_client(tmp_path)

    with client.websocket_connect("/ws") as ws:
        start(ws)
        ask(ws, text)
        assert ws.receive_json()["reason"] == "bad_data"

    assert huri.forwarded() == []


def test_questions_are_rate_limited(tmp_path):
    client, huri = make_client(tmp_path, huri=FakeHuRI(answer=None))

    with client.websocket_connect("/ws") as ws:
        start(ws)
        for i in range(10):
            ask(ws, f"question {i}")
        ask(ws, "one too many")
        # A frame that is always rejected, so a missing rate limit fails the
        # test instead of hanging it on a reply that never comes.
        ws.send_json({"topic": "sentinel", "data": {}})
        assert ws.receive_json() == {
            "type": "rejected",
            "reason": "rate_limited",
            "topic": "question",
        }
        assert ws.receive_json()["topic"] == "sentinel"

    assert len(huri.forwarded()) == 10


def test_session_time_limit(tmp_path):
    client, _ = make_client(tmp_path, session_seconds=0.2)

    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]
        assert ws.receive_json() == {"type": "ended", "reason": "time_limit"}

    # Running out of time still leads to the questionnaire.
    res = client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    assert res.status_code == 201
    assert stored(tmp_path)[0]["end_reason"] == "time_limit"


def test_public_config_hides_the_pipeline(tmp_path):
    client, _ = make_client(tmp_path)
    config = client.get("/api/config").json()

    assert config["access_code_required"] is False
    assert [p["id"] for p in config["presets"]] == ["spoken", "text"]
    for preset in config["presets"]:
        assert set(preset) == {"id", "label", "description"}
    assert [q["id"] for q in config["questionnaires"]] == ["godspeed", "rosas"]


def test_page_is_served(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "/static/app.js" in res.text
    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    # Browsers must revalidate, or a deploy leaves visitors on stale JS/CSS.
    assert res.headers["cache-control"] == asset.headers["cache-control"]
    assert res.headers["cache-control"] == "no-cache"
