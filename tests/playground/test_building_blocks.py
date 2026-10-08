import json

import pytest

from src.core.dataclasses.config import ClientConfig
from src.modules.modules import get_modules
from src.playground.limits import RateLimiter, SessionSlots
from src.playground.presets import load_presets
from src.playground.questionnaire import InvalidAnswers, load_questionnaires
from src.playground.store import AlreadyAnswered, ResponseStore, UnknownSession


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _with_fallbacks():
    for preset in load_presets().values():
        yield pytest.param(preset, id=preset.id)
        if preset.optional:
            yield pytest.param(preset.without(preset.optional), id=f"{preset.id}-min")


@pytest.mark.parametrize("preset", _with_fallbacks())
def test_presets_match_what_huri_serves(preset):
    """A preset naming a module HuRI does not register, or a topic no module
    of the preset consumes or emits, only fails once a visitor clicks start."""
    modules = get_modules()
    names = [m["name"] for m in preset.modules.values()]
    assert set(names) <= set(modules), names

    consumed = {modules[n].input_type for n in names}
    produced = {modules[n].output_type for n in names}
    assert set(preset.inbound) <= consumed
    assert set(preset.outbound) <= produced

    config = ClientConfig.from_dict(preset.handshake("u", "ws://huri/session"))
    assert set(config.modules) == set(preset.modules)


def test_optional_module_is_dropped_with_its_topics():
    spoken = load_presets()["spoken"]
    assert spoken.missing_optional(["rag", "tts", "mov"]) == set()
    assert spoken.missing_optional(["rag", "tts"]) == {"mov"}

    lite = spoken.without({"mov"})
    assert set(lite.modules) == {"rag", "tts"}
    assert lite.outbound == ["token", "audio.out"]
    assert lite.optional == {}


def test_optional_module_must_be_in_the_preset(tmp_path):
    path = tmp_path / "presets.yaml"
    path.write_text(
        "x:\n  label: X\n  description: x\n  inbound: {}\n  outbound: [token]\n"
        "  modules: {rag: {name: rag}}\n  optional: {mov: [motion]}\n"
    )
    with pytest.raises(ValueError, match="mov"):
        load_presets(path)


def test_questionnaire_items():
    godspeed, rosas = load_questionnaires(["godspeed", "rosas"])
    assert (godspeed.points, len(godspeed.item_ids)) == (5, 23)
    assert (rosas.points, len(rosas.item_ids)) == (7, 18)


def test_every_item_feeds_a_score():
    for q in load_questionnaires(["godspeed", "rosas"]):
        assert set().union(*q.scores.values()) == set(q.item_ids), q.id


def test_godspeed_dimension_scores():
    [godspeed] = load_questionnaires(["godspeed"])
    assert list(godspeed.scores) == [
        "anthropomorphism",
        "animacy",
        "likeability",
        "perceived_intelligence",
        "perceived_safety",
    ]

    answers = {item: 1 for item in godspeed.item_ids}
    answers.update(like=5, friendly=5, kind=5, pleasant=5, nice=5, lifelike=4)
    scores = godspeed.score(answers)

    assert scores["likeability"] == 5.0
    # Artificial/Lifelike counts toward both of its dimensions.
    assert scores["anthropomorphism"] == round((4 + 1 * 4) / 5, 2)
    assert scores["animacy"] == round((4 + 1 * 5) / 6, 2)
    assert scores["perceived_safety"] == 1.0


def test_questionnaire_validation_returns_items_in_order():
    [rosas] = load_questionnaires(["rosas"])
    answers = {item: 7 for item in reversed(rosas.item_ids)}
    assert list(rosas.validate(answers)) == rosas.item_ids

    with pytest.raises(InvalidAnswers):
        rosas.validate([7] * len(rosas.item_ids))


def test_session_slots():
    slots = SessionSlots(2)
    assert slots.try_acquire() and slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()


def test_rate_limiter_window_slides():
    clock = Clock()
    limiter = RateLimiter(2, clock=clock)
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()

    clock.now = 59.9
    assert not limiter.allow()
    clock.now = 60.0
    assert limiter.allow()


def test_store_tickets_expire(tmp_path):
    clock = Clock(1000.0)
    store = ResponseStore(tmp_path, ticket_ttl_s=60, clock=clock)
    store.open_session("old", "text")

    clock.now += 61
    with pytest.raises(UnknownSession):
        store.submit("old", {}, "")


def test_store_records_conversation_length(tmp_path):
    clock = Clock(1000.0)
    store = ResponseStore(tmp_path, clock=clock)
    record = store.open_session("s", "text")
    record.questions = 4
    clock.now += 95
    store.close_session("s", "finished")
    clock.now += 300  # time spent on the questionnaire does not count

    store.submit("s", {"rosas": {"happy": 6}}, "")
    with pytest.raises(AlreadyAnswered):
        store.submit("s", {"rosas": {"happy": 6}}, "")

    entry = json.loads((tmp_path / "responses.jsonl").read_text())
    assert entry["conversation_seconds"] == 95.0
    assert entry["questions_asked"] == 4
    assert entry["answers"] == {"rosas": {"happy": 6}}
