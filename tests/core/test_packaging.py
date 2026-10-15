"""The [server]/[client] split: a client never pulls in Ray or FastAPI."""

import re
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from huri_core.core.manifest import loads_toml  # tomli on Python 3.10

ROOT = Path(__file__).resolve().parents[2]
PROJECT = loads_toml((ROOT / "pyproject.toml").read_text())["project"]
EXTRAS = PROJECT["optional-dependencies"]
SERVER_ONLY = {"ray", "fastapi"}

# What a client-side program imports: the base classes and the client SDK.
CLIENT_IMPORTS = [
    "huri_core.core.events",
    "huri_core.core.module",
    "huri_core.core.bus",
    "huri_core.core.session",
    "huri_core.core.client",
    "huri_core.core.dataclasses.config",
]


def resolve(extra: str) -> list[Requirement]:
    """Requirements of an extra, following huri-core[...] self-references."""
    reqs = []
    for raw in EXTRAS[extra]:
        req = Requirement(raw)
        if canonicalize_name(req.name) == canonicalize_name(PROJECT["name"]):
            for sub in req.extras:
                reqs += resolve(sub)
        else:
            reqs.append(req)
    return reqs


def names(reqs: list[Requirement]) -> set[str]:
    return {canonicalize_name(r.name) for r in reqs}


def test_base_install_has_no_dependency():
    assert PROJECT["dependencies"] == []


def test_client_extras_exclude_server_stack():
    assert not names(resolve("client")) & SERVER_ONLY


def test_server_extra_owns_the_serve_pins():
    pinned = {canonicalize_name(r.name): r for r in resolve("server")}
    assert {"ray", "fastapi", "protobuf", "setuptools"} <= pinned.keys()
    assert "4.25.8" in pinned["protobuf"].specifier


def test_client_side_imports_without_ray_or_fastapi():
    # A None entry in sys.modules makes `import ray` raise ImportError, as if
    # only huri-core[client] were installed.
    code = (
        "import sys\n"
        + "".join(f"sys.modules[{m!r}] = None\n" for m in sorted(SERVER_ONLY))
        + "".join(f"import {m}\n" for m in CLIENT_IMPORTS)
    )
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


def constraints() -> dict[str, Requirement]:
    reqs = {}
    for line in (ROOT / "constraints.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            reqs[canonicalize_name(req.name)] = req
    return reqs


def test_constraints_only_pin_what_huri_core_requires():
    """constraints.txt is core's: a module's pins belong in huri-modules."""
    required = {canonicalize_name(r.name) for extra in EXTRAS for r in resolve(extra)}
    assert constraints().keys() <= required, constraints().keys() - required


def test_extras_agree_with_constraint_pins():
    """constraints.txt pins exact versions; extras must accept those pins."""
    pins = {
        name: str(next(iter(req.specifier))).removeprefix("==")
        for name, req in constraints().items()
        if re.fullmatch(r"==[^,*]+", str(req.specifier))
    }
    assert pins, "constraints.txt pins nothing exactly"
    for extra in EXTRAS:
        for req in resolve(extra):
            version = pins.get(canonicalize_name(req.name))
            if version and req.specifier:
                assert version in req.specifier, f"[{extra}] {req} vs =={version}"
