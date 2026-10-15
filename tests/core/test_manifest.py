"""huri_module.toml: parsing, discovery and the compatibility check."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from huri_core.core import plugins
from huri_core.core.compat import CompatibilityError, check_manifests
from huri_core.core.events import JsonEvent
from huri_core.core.manifest import ManifestError, loads_manifest, parse_manifest
from huri_core.core.module import Module

FULL = """
name = "servo"
summary = "Gesture to servo angle arrays"
input = { topic = "huri.motion", event = "huri_events.motion:Motion" }
output = { topic = "acme.servo_angles", event = "tests.core.test_manifest:Angles" }
events = ["huri-events>=0.1,<0.2", "acme-events>=1.0"]
requires = { gpu = false, actuation = true }
huri_core = ">=0.4.0.dev0,<0.5"
"""


@dataclass
class Angles(JsonEvent):
    values: list


class Motion(JsonEvent):
    pass


Motion.__module__, Motion.__qualname__ = "huri_events.motion", "Motion"


class Servo(Module):
    input_type, output_type = "huri.motion", "acme.servo_angles"
    output_event = Angles


def test_full_manifest():
    m = loads_manifest(FULL)
    assert (m.name, m.input.topic, m.output.topic) == (
        "servo",
        "huri.motion",
        "acme.servo_angles",
    )
    assert m.events == ["huri-events>=0.1,<0.2", "acme-events>=1.0"]
    assert (m.gpu, m.actuation, m.huri_core) == (False, True, ">=0.4.0.dev0,<0.5")


def test_minimal_manifest_for_a_terminal_module():
    m = parse_manifest(
        {
            "name": "logger",
            "summary": "Logs tokens",
            "input": {"topic": "huri.token", "event": "huri_events.text:Token"},
            "huri_core": ">=0.4,<0.5",
        }
    )
    assert m.output is None and m.events == [] and not m.gpu and not m.actuation


def test_every_problem_is_reported_at_once():
    with pytest.raises(ManifestError) as exc:
        parse_manifest(
            {
                "name": "Bad Name",
                "summary": "",
                "output": {"topic": "motion", "event": "not a path", "x": 1},
                "events": ["huri-events>>1"],
                "requires": {"gpu": "yes", "wings": True},
                "huri_core": "~~0.4",
                "colour": "blue",
            },
            source="pkg/huri_module.toml",
        )
    text = str(exc.value)
    for fragment in (
        "pkg/huri_module.toml",
        "unknown keys ['colour']",
        "'name'",
        "'summary'",
        "'input' is required",
        "'output.topic'",
        "'output.event'",
        "'output' has unknown keys ['x']",
        "not a valid requirement",
        "requires.gpu",
        "requires.wings",
        "not a valid version range",
    ):
        assert fragment in text, fragment


def test_invalid_toml():
    with pytest.raises(ManifestError, match="invalid TOML"):
        loads_manifest("name = ")


# --- discovery -------------------------------------------------------------------


def write_package(root: Path, name: str, manifest: str) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "module.py").write_text("raise RuntimeError('must not be imported')\n")
    (pkg / "huri_module.toml").write_text(manifest)


def test_manifest_of_an_import_path_is_found_without_importing(tmp_path, monkeypatch):
    write_package(tmp_path, "acme_servo_pkg", FULL)
    monkeypatch.syspath_prepend(str(tmp_path))
    manifest = plugins.find_manifest("acme_servo_pkg.module:Servo")
    assert manifest is not None and manifest.name == "servo"
    assert manifest.source.endswith("acme_servo_pkg/huri_module.toml")


def test_manifest_of_an_installed_module_comes_from_its_distribution(
    tmp_path, monkeypatch
):
    (tmp_path / "huri_module.toml").write_text(FULL)
    dist = SimpleNamespace(
        name="acme-servo",
        files=[Path("huri_module.toml")],
        locate_file=lambda f: tmp_path / f,
    )
    ep = SimpleNamespace(
        name="servo", value="not_installed_xyz.module:Servo", dist=dist
    )
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep])
    manifest = plugins.find_manifest("servo")
    assert manifest is not None and manifest.summary.startswith("Gesture")


def test_module_without_manifest(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda group: [])
    assert plugins.find_manifest("servo") is None
    assert plugins.find_manifest("json.decoder:JSONDecoder") is None


# --- compatibility -----------------------------------------------------------------

VERSIONS = {"huri-core": "0.4.0.dev0", "huri-events": "0.1.0", "acme-events": "1.2"}
EVENTS = {"huri.motion": Motion, "acme.servo_angles": Angles}


def check(manifest_text=FULL, versions=VERSIONS, module=Servo):
    check_manifests(
        {"servo": module},
        {"servo": loads_manifest(manifest_text, "servo.toml")},
        EVENTS,
        installed_version=versions.get,
    )


def test_matching_manifest_passes():
    check()


def test_topic_mismatch_with_the_module_class():
    class Renamed(Servo):
        output_type = "acme.angles"

    with pytest.raises(CompatibilityError, match="output topic is 'acme.servo_angles'"):
        check(module=Renamed)


def test_event_class_mismatch():
    text = FULL.replace("tests.core.test_manifest:Angles", "acme_events:OldAngles")
    with pytest.raises(CompatibilityError, match="OldAngles.*Angles is what carries"):
        check(text)


def test_huri_core_out_of_range():
    with pytest.raises(CompatibilityError, match="supports huri-core .* 0.5.1 is"):
        check(versions={**VERSIONS, "huri-core": "0.5.1"})


def test_event_package_missing_or_out_of_range():
    with pytest.raises(CompatibilityError) as exc:
        check(versions={"huri-core": "0.4.0", "huri-events": "0.2.0"})
    assert len(exc.value.problems) == 2
    assert "huri-events>=0.1,<0.2, but 0.2.0" in str(exc.value)
    assert "acme-events>=1.0, which is not installed" in str(exc.value)


def test_manifest_without_output_for_a_producing_module():
    text = "\n".join(
        line for line in FULL.splitlines() if not line.startswith("output")
    )
    with pytest.raises(CompatibilityError, match="no output"):
        check(text)


def test_import_path_of_an_installed_module_uses_its_entry_point_manifest(
    tmp_path, monkeypatch
):
    """Enabling a packaged module by import path must not skip its manifest
    (its requires.actuation, its version ranges)."""
    (tmp_path / "huri_module.toml").write_text(FULL)
    dist = SimpleNamespace(
        name="acme-servo",
        files=[Path("huri_module.toml")],
        locate_file=lambda f: tmp_path / f,
    )
    ep = SimpleNamespace(
        name="servo", value="acme_driver.module:ServoDriver", dist=dist
    )
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep])
    manifest = plugins.find_manifest("acme_driver.module:ServoDriver")
    assert manifest is not None and manifest.name == "servo" and manifest.actuation


def test_import_path_lookup_never_wanders_into_other_packages(tmp_path, monkeypatch):
    """Only the module's own directory is read, not its whole tree."""
    namespace = tmp_path / "ns"
    namespace.mkdir()
    write_package(namespace, "other_pkg", FULL.replace('"servo"', '"tts"'))
    (namespace / "mine.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(plugins, "entry_points", lambda group: [])
    assert plugins.find_manifest("ns.mine:TTS") is None
    assert plugins.find_manifest("ns.missing:TTS") is None
