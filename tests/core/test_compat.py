"""Startup and per-session compatibility checks (huri_core.core.compat)."""

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from huri_core.core.compat import CompatibilityError, check_modules, check_session
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.events import JsonEvent
from huri_core.core.module import Module
from huri_core.core.plugins import installed_modules, load_events, load_modules

ROOT = Path(__file__).resolve().parents[2]


class Angles(JsonEvent):
    pass


class OldAngles(JsonEvent):
    pass


class Servo(Module):
    input_type = "huri.motion"
    output_type = "acme.servo_angles"
    output_event = Angles


class Untyped(Module):
    input_type = "huri.token"
    output_type = None


def stub(cls):
    """An instance without running __init__ (no handle, no model)."""
    return cls.__new__(cls)


# --- startup: modules vs installed event packages ---------------------------


def test_compatible_modules_pass():
    check_modules(
        {"servo": Servo}, {"huri.motion": Angles, "acme.servo_angles": Angles}
    )


def test_missing_curated_topic_names_the_curated_events():
    with pytest.raises(
        CompatibilityError, match="'servo' consumes 'huri.motion'.*huri-events"
    ):
        check_modules({"servo": Servo}, {"acme.servo_angles": Angles})


def test_missing_third_party_topic_names_its_namespace():
    with pytest.raises(CompatibilityError, match="'acme' namespace"):
        check_modules({"servo": Servo}, {"huri.motion": Angles})


def test_event_class_mismatch_names_both_classes():
    events = {"huri.motion": Angles, "acme.servo_angles": OldAngles}
    with pytest.raises(CompatibilityError, match="Angles.*OldAngles.*compatible"):
        check_modules({"servo": Servo}, events)


def test_all_problems_are_reported_at_once():
    with pytest.raises(CompatibilityError) as exc:
        check_modules({"servo": Servo, "untyped": Untyped}, {})
    assert len(exc.value.problems) == 3


def test_curated_modules_fit_the_installed_events():
    check_modules(load_modules(installed_modules()), load_events())


# --- per session: wiring -----------------------------------------------------


def test_input_without_producer_is_rejected():
    with pytest.raises(CompatibilityError, match="Servo consumes 'huri.motion'"):
        check_session([stub(Servo)], sent=[], subscribed=[])


def test_input_is_produced_by_the_client_or_another_module():
    check_session([stub(Servo)], sent=["huri.motion"], subscribed=["acme.servo_angles"])

    class Gesture(Module):
        input_type = "huri.token"
        output_type = "huri.motion"

    check_session([stub(Gesture), stub(Servo)], sent=["huri.token"], subscribed=[])


def test_subscription_to_a_topic_nobody_emits_is_rejected():
    with pytest.raises(CompatibilityError, match="hook topic.*acme.lights"):
        check_session([stub(Servo)], sent=["huri.motion"], subscribed=["acme.lights"])


@pytest.mark.parametrize(
    "path",
    sorted(
        p for p in (ROOT / "config").glob("client_*.yaml") if "template" not in p.name
    ),
    ids=lambda p: p.name,
)
def test_shipped_client_configs_are_complete_sessions(path):
    raw = OmegaConf.to_container(OmegaConf.load(path), resolve=True)
    config = ClientConfig.from_dict({**raw, "user_id": "test"})
    modules = load_modules(installed_modules())
    pipeline = [stub(modules[m.key]) for m in config.modules.values()]
    check_session(
        pipeline,
        sent=config.send,
        subscribed=config.subscribe,
    )
