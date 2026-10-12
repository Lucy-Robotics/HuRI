from collections import defaultdict
from typing import Dict, Optional

from huri_core.core.module import Module
from huri_core.modules.rag.events import PartialQuestion

from .events import Emotion


class EAG(Module):
    """EAG Module

    Aggregate all emotions and send when voice end.

    input: huri.emotion,
    output: huri.partial_question

    :ema_alpha: if not None, the aggragation will use ema computation instead
    of average. Recents emotion will have stronger impact on the final score.
    Lower alpha will make impact lower, and higher alpha will make it higher. \
    Default alpha would be ~0.3.
    """

    input_type = "huri.emotion"
    output_type = "huri.partial_question"

    def __init__(self, ema_alpha: Optional[float] = None):
        super().__init__()

        self.scores: Dict[str, float] = defaultdict(float)
        self.count: int = 0

        self.ema_alpha = ema_alpha

    def _finalize(self) -> Emotion:
        avg_scores = (
            {label: score / self.count for label, score in self.scores.items()}
            if self.ema_alpha is None
            else self.scores
        )

        best_label = max(avg_scores, key=lambda label: avg_scores[label])

        result = Emotion(
            label=best_label,
            confidence=avg_scores[best_label],
            scores=avg_scores,
            end=True,
        )

        self.scores.clear()
        self.count = 0

        return result

    async def process(self, emotion: Emotion) -> Optional[PartialQuestion]:
        if self.ema_alpha is not None:
            for label, score in emotion.scores.items():
                self.scores[label] = (
                    self.ema_alpha * score + (1 - self.ema_alpha) * self.scores[label]
                )
        else:
            for label, score in emotion.scores.items():
                self.scores[label] += score

        self.count += 1

        if emotion.end:
            emotion = self._finalize()

            return PartialQuestion(transcript=None, emotion=emotion)

        return None
