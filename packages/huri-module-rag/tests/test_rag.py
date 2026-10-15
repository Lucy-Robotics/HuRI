"""RAG and QAG on fake deployments, through huri_core.testing."""

import asyncio

from huri_events.emotion import Emotion
from huri_events.question import PartialQuestion, RAGQuestion
from huri_events.speech import Transcript
from huri_events.text import Token
from huri_rag.question_aggregator import QAG
from huri_rag.rag import RAG

from huri_core.testing import LocalHandle, ModuleHarness


def test_a_real_streaming_module_runs_on_a_fake_deployment():
    class FakeRAGHandle:
        async def stream(self, query):
            for delta in ("Hello", " ", query.question):
                yield delta

        async def save_conversation(self, user_id, history):
            self.saved = (user_id, history)

    fake = FakeRAGHandle()
    harness = ModuleHarness(RAG, handle=LocalHandle(fake), user_id="alice")
    out = harness.run(RAGQuestion(Transcript("world", True), None))
    assert out == [
        Token("Hello", False),
        Token(" ", False),
        Token("world", False),
        Token("", True),
    ]

    asyncio.run(harness.finalize())
    assert fake.saved[0] == "alice" and fake.saved[1][-1]["content"] == "Hello world"


def test_qag_waits_for_the_emotion_when_asked_to():
    harness = ModuleHarness(QAG, use_emotion=True)
    emotion = Emotion("joy", 0.8, {"joy": 0.8}, True)
    assert harness.run(PartialQuestion(Transcript("hi", True), None)) == []
    assert harness.run(PartialQuestion(None, emotion)) == [
        RAGQuestion(Transcript("hi", True), emotion)
    ]


def test_qag_without_emotion_forwards_the_transcript():
    out = ModuleHarness(QAG, use_emotion=False).run(
        PartialQuestion(Transcript("hi", True), None)
    )
    assert out == [RAGQuestion(Transcript("hi", True), None)]
