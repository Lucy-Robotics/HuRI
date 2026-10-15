import asyncio

import pytest
from huri_events.speech import Transcript
from huri_tag.module import TAG

from huri_core.testing import ModuleHarness


@pytest.mark.parametrize(
    "current,new,expected",
    [
        (
            "what is the weather",
            "the weather like in Paris today",
            "what is the weather like in Paris today",
        ),
        # No overlap must never corrupt/replace the sentence.
        (
            "what is the weather like",
            "in Paris today",
            "what is the weather like in Paris today",
        ),
        (
            "What is the weather like.",
            "weather like in Paris today?",
            "What is the weather like in Paris today?",
        ),
        (
            "I want to book a table for two",
            "for two people at eight",
            "I want to book a table for two people at eight",
        ),
        ("it is a very very", "very very long day", "it is a very very long day"),
        # Word cut by the window edge.
        ("what is the weath", "weather like", "what is the weather like"),
        # New text fully contained in the tail.
        ("hello there my friend", "my friend", "hello there my friend"),
        ("", "hello", "hello"),
        ("hello", "", "hello"),
    ],
)
def test_merge(current, new, expected):
    assert TAG()._merge(current, new) == expected


def test_tag_emits_on_end_and_resets():
    out = ModuleHarness(TAG).run(
        Transcript("what is the", False),
        Transcript("the weather", False),
        Transcript("weather like", True),
        Transcript("", True),
        Transcript("next question", True),
    )
    assert [q.transcript.text for q in out] == [
        "what is the weather like",
        "next question",
    ]


def test_stitches_the_overlapping_passes_stt_emits():
    out = ModuleHarness(TAG).run(
        Transcript("what is the", False), Transcript("the weather", True)
    )
    assert [q.transcript.text for q in out] == ["what is the weather"]


def test_min_prefix_is_configurable():
    # "th" (from a cut "the") only matches when min_prefix allows 2 chars.
    assert TAG(min_prefix=3)._merge("what is th", "the weather") == (
        "what is th the weather"
    )
    assert TAG(min_prefix=2)._merge("what is th", "the weather") == (
        "what is the weather"
    )

    async def run():
        tag = TAG(min_prefix=2)
        await tag.process(Transcript("what is th", False))
        q = await tag.process(Transcript("the weather", True))
        assert q.transcript.text == "what is the weather"

    asyncio.run(run())
