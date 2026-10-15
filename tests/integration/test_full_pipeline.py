"""The whole curated pipeline in one session, no Ray and no model:

    audio in -> VAD -> STT -> TAG ┐
                    └> EMO -> EAG ┴> QAG -> RAG -> TTS -> MOV
                                                   │      │
                         client <- huri.token, huri.audio.out, huri.motion

Real module classes from the installed packages, built by the server's module
factory and wired by its event bus; the client side is the server's Sender.
Only the model deployments are fakes (huri_core.testing.LocalHandle).
"""

import asyncio
import json
import struct

import numpy as np
from huri_events import Audio, Motion, Token

from huri_core.core.dataclasses.config import ModuleConfig
from huri_core.core.events import BytesEvent
from huri_core.core.factory import ModuleFactory
from huri_core.core.plugins import load_events, load_modules
from huri_core.core.sender import Sender
from huri_core.core.session import Session
from huri_core.testing import LocalHandle

FRAME = np.full(320, 8000, dtype=np.int16).tobytes()  # 20 ms at 16 kHz


class FakeWhisper:
    async def transcribe(self, audio, language):
        return "hello robot"


class FakeEmotionModel:
    async def predict(self, audio):
        return {"label": "hap", "confidence": 0.9, "scores": {"hap": 0.9}}


class FakeLLM:
    def __init__(self):
        self.questions = []

    async def stream(self, query):
        self.questions.append(query.question)
        for delta in ("You said: ", query.question):
            yield delta

    async def save_conversation(self, user_id, history):
        pass


class FakeVoice:
    """The TTS deployment surface: 10 samples per character."""

    def __init__(self):
        self.sessions = {}

    async def get_sample_rate(self):
        return 16000

    async def start_session(self, sid):
        self.sessions[sid] = ([], asyncio.Event())

    async def push_text(self, sid, text, end):
        texts, done = self.sessions[sid]
        texts.append(text)
        if end:
            done.set()

    async def stream_audio(self, sid):
        texts, done = self.sessions[sid]
        await done.wait()
        for text in texts:
            if text:
                yield Audio(np.zeros(len(text) * 10, dtype=np.float32), 16000)


class FakeGestures:
    async def infer(self, window, sr):
        frames = max(1, int(len(window) / sr * 30))
        return Motion(
            poses=np.zeros((frames, 165), dtype=np.float32),
            expressions=np.zeros((frames, 100), dtype=np.float32),
            trans=np.zeros((frames, 3), dtype=np.float32),
        )


class Scripted:
    """WebRTC's detector, scripted: speech, then silence."""

    def __init__(self, speech_frames):
        self.left = speech_frames

    def is_speech(self, frame, sample_rate):
        self.left -= 1
        return self.left >= 0


class FakeWS:
    """What the server's Sender writes to: the client's socket."""

    def __init__(self):
        self.frames = []

    async def send_bytes(self, payload):
        n = struct.unpack(">H", payload[:2])[0]
        self.frames.append((payload[2 : 2 + n].decode(), payload[2 + n :]))

    async def send_json(self, message):
        self.frames.append((message["topic"], json.loads(json.dumps(message["data"]))))


PIPELINE = {
    "vad": {"silence_duration": 0.06, "block_duration": 0.02},
    "stt": {"transcribe_window": 0.2, "transcribe_step": 0.1, "block_duration": 0.02},
    "tag": {},
    "emo": {"analysis_window": 0.2, "block_duration": 0.02},
    "eag": {},
    "qag": {"use_emotion": True},
    "rag": {},
    "tts": {},
    "mov": {"min_chunk_sec": 0.01},
}
SUBSCRIBED = ["huri.token", "huri.audio.out", "huri.motion"]


def test_a_spoken_question_comes_back_as_text_speech_and_gestures():
    llm = FakeLLM()
    handles = {
        "stt": LocalHandle(FakeWhisper()),
        "emo": LocalHandle(FakeEmotionModel()),
        "rag": LocalHandle(llm),
        "tts": LocalHandle(FakeVoice()),
        "mov": LocalHandle(FakeGestures()),
    }
    factory = ModuleFactory(handles)
    for name, cls in load_modules(list(PIPELINE)).items():
        factory.register(name, cls)
    pipeline = factory.create_from_config(
        "alice",
        {
            n: ModuleConfig.from_dict({"name": n, "args": a})
            for n, a in PIPELINE.items()
        },
    )
    vad = next(m for m in pipeline if type(m).__name__ == "VAD")
    vad.vad = Scripted(speech_frames=15)

    ws = FakeWS()
    session = Session(pipeline + [Sender(ws, topic) for topic in SUBSCRIBED])
    events = load_events()

    def received(topic):
        return [events[t].from_wire(data) for t, data in ws.frames if t == topic]

    async def talk():
        for _ in range(30):  # 15 speech frames, then silence
            await session.publish("huri.audio.in", BytesEvent(FRAME))
            await asyncio.sleep(0.005)
        for _ in range(400):
            done = any(t.end for t in received("huri.token")) and any(
                a.end for a in received("huri.audio.out")
            )
            if done and received("huri.motion"):
                return
            await asyncio.sleep(0.01)

    asyncio.run(asyncio.wait_for(talk(), timeout=20))

    tokens = received("huri.token")
    assert "".join(t.text for t in tokens) == "You said: hello robot"
    assert tokens[-1] == Token("", True)
    assert llm.questions == ["hello robot"]

    audio = received("huri.audio.out")
    assert audio[-1].end and sum(a.data.size for a in audio) > 0
    assert [a.pts for a in audio[:-1]] == sorted(a.pts for a in audio[:-1])

    motion = received("huri.motion")
    assert motion and sum(m.poses.shape[0] for m in motion) > 0
