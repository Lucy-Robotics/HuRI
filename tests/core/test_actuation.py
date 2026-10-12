"""Modules that can move hardware need an explicit, separate opt-in."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from huri_core.core.compat import CompatibilityError, actuating_modules, check_actuation
from huri_core.core.manifest import parse_manifest
from huri_core.core.module import Module

ROOT = Path(__file__).resolve().parents[2]


class Servo(Module):
    """Declares actuation on the class (e.g. an unpackaged, tier-0 module)."""

    input_type, output_type = "huri.motion", None
    actuation = True

    async def process(self, data):
        return None


class Arm(Module):
    """Declares actuation in its manifest only."""

    input_type, output_type = "huri.motion", None


class Logger(Module):
    input_type, output_type = "huri.token", None


ARM_MANIFEST = parse_manifest(
    {
        "name": "arm",
        "summary": "Moves an arm",
        "input": {"topic": "huri.motion", "event": "huri_events.motion:Motion"},
        "requires": {"actuation": True},
        "huri_core": ">=0.4.0.dev0,<0.5",
    }
)
MODULES = {"servo": Servo, "arm": Arm, "logger": Logger}


def test_actuation_is_declared_by_the_class_or_the_manifest():
    assert actuating_modules(MODULES, {"arm": ARM_MANIFEST}) == ["arm", "servo"]


def test_enabled_actuation_modules_are_refused_without_opt_in():
    with pytest.raises(CompatibilityError) as exc:
        check_actuation(MODULES, {"arm": ARM_MANIFEST}, opted_in=["servo"])
    assert exc.value.problems == [
        "module 'arm' can drive physical hardware (actuation): the server's "
        "operator must opt in explicitly by listing it in HURI_ACTUATION_MODULES, "
        "in addition to HURI_MODULES"
    ]


def test_opted_in_modules_run_and_others_are_unaffected():
    assert check_actuation(MODULES, {"arm": ARM_MANIFEST}, ["arm", "servo"]) == [
        "arm",
        "servo",
    ]
    assert check_actuation({"logger": Logger}, {}, []) == []


def build_app(**env):
    """Import the server app the way Serve does, with these settings."""
    return subprocess.run(
        [sys.executable, "-c", "import huri_core.app"],
        cwd=ROOT,
        env={**os.environ, "PYTHONWARNINGS": "ignore", **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_server_refuses_an_enabled_but_not_opted_in_actuation_module():
    servo = f"{__name__}:Servo"
    refused = build_app(HURI_MODULES=f"tag,{servo}")
    assert refused.returncode != 0
    assert "HURI_ACTUATION_MODULES" in refused.stderr

    allowed = build_app(HURI_MODULES=f"tag,{servo}", HURI_ACTUATION_MODULES=servo)
    assert allowed.returncode == 0, allowed.stderr
