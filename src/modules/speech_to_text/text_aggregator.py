import re
from typing import Optional

from src.core.module import Module
from src.modules.rag.events import PartialQuestion

from .events import Transcript


class TAG(Module):
    """TAG Module

    Aggregate all transcriptions and send when transcript end.

    Whisper transcribes each sliding window independently, so the start of each
    transcript repeats the end of the previous one (with possibly different
    casing or punctuation). Transcripts are stitched together on that overlap.

    input: transcript,
    output: partial_question

    :min_prefix: shortest word fragment (in characters) that may match a longer
        word when merging overlapping transcripts, e.g. "weath" and "weather".
        Guards against 1-2 letter fragments matching unrelated words.
    """

    input_type = "transcript"
    output_type = "partial_question"

    def __init__(
        self,
        min_prefix: int = 3,
    ):
        super().__init__()

        self.min_prefix = min_prefix

        self.sentence: str = ""

    @staticmethod
    def _norm(word: str) -> str:
        return re.sub(r"[^\w]", "", word.lower())

    def _same_word(self, a: str, b: str) -> bool:
        """Words match after normalisation, or when one is a truncated form of
        the other (a window edge can cut a word in half: "weath" / "weather")."""

        na, nb = self._norm(a), self._norm(b)
        if na == nb:
            return True
        short, long_ = sorted((na, nb), key=len)
        return len(short) >= self.min_prefix and long_.startswith(short)

    def _merge(self, current: str, new: str) -> str:
        """Find the longest run of words where the tail of ``current`` equals the
        head of ``new`` and append only what follows it. When there is no
        overlap, ``new`` is appended whole: text is never dropped from either
        side.
        """

        cur_words = current.split()
        new_words = new.split()
        if not cur_words:
            return new.strip()
        if not new_words:
            return current.strip()

        for k in range(min(len(cur_words), len(new_words)), 0, -1):
            if all(
                self._same_word(a, b) for a, b in zip(cur_words[-k:], new_words[:k])
            ):
                # Keep the newer spelling of the overlap (more audio context).
                return " ".join(cur_words[:-k] + new_words)
        return " ".join(cur_words + new_words)

    async def process(self, transcript: Transcript) -> Optional[PartialQuestion]:
        text = transcript.text

        if text != "":
            self.sentence = self._merge(self.sentence, text)

        if transcript.end and self.sentence != "":
            transcript = Transcript(self.sentence, True)
            self.sentence = ""
            return PartialQuestion(transcript=transcript, emotion=None)
        else:
            return None
