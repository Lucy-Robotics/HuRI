#!/usr/bin/env python3
"""Check registry.toml against the packages it lists (run by CI on every change).

    python huri-modules/check_registry.py [--run-tests]

For each listed module: its package is where `source` says, provides the module
under that name, ships a manifest that agrees with the entry, passes HuRI's
startup compatibility checks against the installed huri-core and events, and
only produces topics of an event package it declares. Every curated module
(packages/huri-module-*) must be listed. --run-tests also runs each listed
package's own tests (huri_core.testing, no Ray needed).

Needs the listed packages installed (pip install -e), like the server.
"""

import argparse
import datetime as dt
import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Dict, List

from packaging.requirements import Requirement

from huri_core.core.compat import CompatibilityError, check_manifests, check_modules
from huri_core.core.manifest import ManifestError
from huri_core.core.plugins import (
    PluginError,
    find_manifest,
    load_events,
    load_modules,
    topic_namespace,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _pyproject(path: Path) -> Dict[str, Any]:
    project: Dict[str, Any] = tomllib.loads((path / "pyproject.toml").read_text())[
        "project"
    ]
    return project


def _norm(name: str) -> str:
    return name.replace("_", "-").lower()


def check(registry_path: Path, repo: Path) -> List[str]:
    problems: List[str] = []
    registry = tomllib.loads(registry_path.read_text())
    if registry.get("schema") != 1:
        return [f"unsupported schema {registry.get('schema')!r} (expected 1)"]

    # Which distribution registers each event namespace.
    namespace_owner: Dict[str, str] = {}
    for ep in entry_points(group="huri.events"):
        dist = getattr(ep, "dist", None)
        namespace_owner[topic_namespace(ep.name)] = _norm(dist.name) if dist else "?"

    for entry in registry.get("events", []):
        where = f"events {entry.get('package')!r}"
        source = repo / entry.get("source", {}).get("path", "<missing>")
        if not (source / "pyproject.toml").is_file():
            problems.append(f"{where}: no package at source {source}")
            continue
        if _norm(_pyproject(source)["name"]) != _norm(entry["package"]):
            problems.append(f"{where}: {source} is {_pyproject(source)['name']}")
        owner = namespace_owner.get(entry.get("namespace", ""))
        if owner != _norm(entry["package"]):
            problems.append(
                f"{where}: namespace {entry.get('namespace')!r} is registered by "
                f"{owner or 'no installed package'}"
            )

    listed: Dict[str, Dict[str, Any]] = {}
    for entry in registry.get("module", []):
        name = entry.get("name", "<missing>")
        where = f"module {name!r}"
        if name in listed:
            problems.append(f"{where}: listed twice")
            continue
        listed[name] = entry
        if not isinstance(entry.get("verified"), dt.date):
            problems.append(f"{where}: 'verified' must be a date (YYYY-MM-DD)")
        source = repo / entry.get("source", {}).get("path", "<missing>")
        if not (source / "pyproject.toml").is_file():
            problems.append(f"{where}: no package at source {source}")
            continue
        project = _pyproject(source)
        if _norm(project["name"]) != _norm(entry.get("package", "")):
            problems.append(
                f"{where}: {source} is {project['name']}, not {entry.get('package')}"
            )
        provided = project.get("entry-points", {}).get("huri.modules", {})
        if name not in provided:
            problems.append(f"{where}: {project['name']} provides {sorted(provided)}")
            continue
        try:
            manifest = find_manifest(name)
        except ManifestError as e:
            problems.append(f"{where}: {e}")
            continue
        if manifest is None:
            problems.append(
                f"{where}: {project['name']} ships no huri_module.toml for it"
            )
            continue
        for key in ("summary", "huri_core"):
            if getattr(manifest, key) != entry.get(key):
                problems.append(
                    f"{where}: registry {key} {entry.get(key)!r} differs from the "
                    f"manifest's {getattr(manifest, key)!r}"
                )
        if manifest.output is not None:
            owner = namespace_owner.get(topic_namespace(manifest.output.topic))
            declared = {_norm(Requirement(r).name) for r in manifest.events}
            if owner not in declared:
                problems.append(
                    f"{where}: produces {manifest.output.topic!r}, registered by "
                    f"{owner or 'no installed package'}, which its manifest's "
                    f"events {manifest.events} do not declare"
                )

    curated = set()
    for path in sorted((repo / "packages").glob("huri-module-*")):
        curated |= set(_pyproject(path).get("entry-points", {}).get("huri.modules", {}))
    for name in sorted(curated - set(listed)):
        problems.append(f"curated module {name!r} (packages/) is not in the registry")

    try:
        modules = load_modules(sorted(listed))
        events = load_events()
        check_modules(modules, events)
        manifests = {}
        for name in modules:
            manifest = find_manifest(name)
            if manifest is not None:
                manifests[name] = manifest
        check_manifests(modules, manifests, events)
    except (PluginError, ValueError, CompatibilityError) as e:
        problems.append(f"startup compatibility: {e}")
    return problems


def run_tests(registry_path: Path, repo: Path) -> List[str]:
    registry = tomllib.loads(registry_path.read_text())
    sources = {
        e["source"]["path"]
        for e in registry.get("module", []) + registry.get("events", [])
        if "path" in e.get("source", {})
    }
    failed = []
    for source in sorted(sources):
        tests = repo / source / "tests"
        if not tests.is_dir():
            continue
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(tests)], cwd=repo
        )
        if res.returncode != 0:
            failed.append(f"{source}: tests failed")
    return failed


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", type=Path, default=HERE / "registry.toml")
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args(argv)

    problems = check(args.registry, REPO)
    if args.run_tests and not problems:
        problems = run_tests(args.registry, REPO)
    for problem in problems:
        print(f"- {problem}")
    print("registry OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
