"""templates/huri-plugin-template stays a working starting point."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from huri_core.core.manifest import load_manifest
from huri_core.testing import ModuleHarness

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "huri-plugin-template"


@pytest.fixture
def template_on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(TEMPLATE / "events"))
    monkeypatch.syspath_prepend(str(TEMPLATE))
    yield
    for name in [
        m for m in sys.modules if m.startswith(("huri_example", "huri_events_example"))
    ]:
        del sys.modules[name]


def test_the_example_module_works(template_on_path):
    from huri_events.speech import Transcript
    from huri_events_example import Shout
    from huri_example.module import Shouter

    out = ModuleHarness(Shouter).run(Transcript("hi", True))
    assert out == [Shout("HI!")]


def test_its_manifest_matches_the_module(template_on_path):
    from huri_example.module import Shouter

    manifest = load_manifest(TEMPLATE / "huri_example" / "huri_module.toml")
    assert manifest.name == "example"
    assert (manifest.input.topic, manifest.output.topic) == (
        Shouter.input_type,
        Shouter.output_type,
    )
    for port, cls in (
        (manifest.input, Shouter.input_event),
        (manifest.output, Shouter.output_event),
    ):
        assert port.event == f"{cls.__module__}:{cls.__qualname__}"


def test_rename_leaves_no_trace_of_the_example(tmp_path):
    copy = tmp_path / "huri-module-servo"
    shutil.copytree(TEMPLATE, copy)
    subprocess.run(
        ["bash", str(copy / "rename.sh"), "servo"], check=True, capture_output=True
    )
    assert (copy / "huri_servo" / "module.py").is_file()
    assert (copy / "events" / "huri_events_servo" / "__init__.py").is_file()
    # The module itself is renamed: its entry point and manifest.
    assert (
        'servo = "huri_servo.module:Shouter"' in (copy / "pyproject.toml").read_text()
    )
    assert 'name = "servo"' in (copy / "huri_servo" / "huri_module.toml").read_text()
    leftovers = subprocess.run(
        ["grep", "-rlI", "--exclude=rename.sh", "-i", "example", str(copy)],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert leftovers == []


@pytest.mark.parametrize("script", ["install.sh", "rename.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(TEMPLATE / script)], check=True)
