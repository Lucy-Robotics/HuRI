from typing import Optional

from huri_events.speech import Transcript
from huri_events_example import Shout

from huri_core.core.module import Module


class Shouter(Module):
    """Turns each finished transcript into a shout.

    input: huri.transcript (Transcript)
    output: example.shout (Shout)

    :exclamations: how many "!" to add.
    """

    input_type = "huri.transcript"
    output_type = "example.shout"
    input_event = Transcript
    output_event = Shout

    def __init__(self, exclamations: int = 1):
        self.exclamations = exclamations

    async def process(self, data: Transcript) -> Optional[Shout]:
        # Called once per event on input_type; return None to emit nothing.
        # A generator (async def + yield) may emit several events per input.
        if not data.end or not data.text.strip():
            return None
        return Shout(data.text.upper() + "!" * self.exclamations)
