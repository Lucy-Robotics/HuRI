"""The curated module packages (packages/huri-module-*) are consistent."""

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from huri_core.core.compat import check_manifests, check_modules
from huri_core.core.plugins import find_manifest, load_events, load_modules

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted((ROOT / "packages").glob("huri-module-*"))


def project(path):
    return tomllib.loads((path / "pyproject.toml").read_text())["project"]


def pins():
    found = {}
    for name in ("requirements.txt", "serve_requirements.txt", "constraints.txt"):
        for line in (ROOT / name).read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9_.\-\[\]]+)==([^\s#]+)", line)
            if match:
                found[canonicalize_name(Requirement(match[1]).name)] = match[2]
    return found


@pytest.mark.parametrize("path", PACKAGES, ids=lambda p: p.name)
def test_every_module_ships_a_matching_manifest(path):
    names = list(project(path)["entry-points"]["huri.modules"])
    modules = load_modules(names)
    manifests = {name: find_manifest(name) for name in names}
    assert all(manifests.values()), manifests
    events = load_events()
    check_modules(modules, events)
    check_manifests(modules, manifests, events)


@pytest.mark.parametrize("path", PACKAGES, ids=lambda p: p.name)
def test_dependencies_accept_the_repo_pins(path):
    repo_pins = pins()
    for raw in project(path)["dependencies"]:
        req = Requirement(raw)
        pinned = repo_pins.get(canonicalize_name(req.name))
        if pinned and req.specifier:
            assert pinned in req.specifier, f"{path.name}: {raw} vs =={pinned}"


def test_each_curated_module_lives_in_exactly_one_package():
    seen = {}
    for path in PACKAGES:
        for name in project(path)["entry-points"]["huri.modules"]:
            assert name not in seen, f"{name} in {seen.get(name)} and {path.name}"
            seen[name] = path.name
    assert set(seen) == {
        "vad",
        "stt",
        "tag",
        "emo",
        "eag",
        "qag",
        "rag",
        "mov",
        "tts",
        "tts_cosyvoice",
    }
