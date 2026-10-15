"""Startup and per-session compatibility checks (huri_core.core.compat).

`huri.*` topics appear as plain strings: the checks treat the curated
namespace specially (which package to install), not the curated events.
"""

import pytest

from huri_core.core.compat import CompatibilityError, check_modules, check_session
from huri_core.core.events import JsonEvent
from huri_core.core.module import Module


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
