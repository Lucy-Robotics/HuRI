from typing import Optional

from huri_core.core.module import Module
from huri_core.modules.emotion.events import Emotion
from huri_core.modules.speech_to_text.events import Transcript

from .events import PartialQuestion, RAGQuestion


class QAG(Module):
    """QAG Module

    Aggregate sentence and emotion into a RAGQuestion.

    input: huri.partial_question,
    output: huri.question

    :use_emotion: default True.
    Set to False if you do not analyze emotion or do not need it.
    """

    input_type = "huri.partial_question"
    output_type = "huri.question"
    input_event = PartialQuestion
    output_event = RAGQuestion

    def __init__(self, use_emotion: bool = True):
        super().__init__()

        self.current_transcript: Optional[Transcript] = None
        self.current_emotion: Optional[Emotion] = None

        self.use_emotion = use_emotion

    async def process(self, partial_question: PartialQuestion) -> Optional[RAGQuestion]:
        if partial_question.emotion is not None:
            self.current_emotion = partial_question.emotion

        if partial_question.transcript is not None:
            self.current_transcript = partial_question.transcript

        if self.current_transcript is None:
            return None
        # With emotion enabled, hold the question back until its emotion lands.
        if self.use_emotion and self.current_emotion is None:
            return None

        emotion = self.current_emotion if self.use_emotion else None
        question = RAGQuestion(self.current_transcript, emotion)

        # Clear the aggregation state after firing. Otherwise a later partial
        # carrying only one half (the next turn's emotion-only update, or a
        # stray transcript) re-emits this same question with stale data — which
        # made the avatar answer the previous turn again. TAG/EAG already reset
        # their own state on emit; QAG must do the same.
        self.current_transcript = None
        self.current_emotion = None
        return question
