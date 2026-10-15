"""`huri_module.toml`: what a module is, readable without importing it.

A module package ships one manifest per module it registers, next to its code:

    name = "servo"                                   # its huri.modules entry point
    summary = "Gesture to servo angle arrays"
    input = { topic = "huri.motion", event = "huri_events.motion:Motion" }
    output = { topic = "acme.servo_angles", event = "huri_events_servo:ServoAngles" }
    events = ["huri-events>=0.2,<0.3", "huri-events-servo>=0.1,<0.2"]
    requires = { gpu = false, actuation = false }
    huri_core = ">=0.3,<0.4"

`output` is omitted by a terminal module; `events` and `requires` are optional.
Tooling (the startup compatibility check, huri-modules' hub) reads it to
reason about a module before, or without, loading it.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

MANIFEST_FILE = "huri_module.toml"

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TOPIC = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")
_IMPORT_PATH = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
_KEYS = {"name", "summary", "input", "output", "events", "requires", "huri_core"}
_REQUIRES = {"gpu", "actuation"}


class ManifestError(ValueError):
    """A manifest is malformed; lists every problem found."""

    def __init__(self, source: str, problems: List[str]):
        self.source = source
        self.problems = problems
        super().__init__(f"{source}:\n" + "\n".join(f"- {p}" for p in problems))


@dataclass(frozen=True)
class Port:
    """A topic a module consumes or produces, and the event class on it."""

    topic: str
    event: str  # "package.module:Class"


@dataclass(frozen=True)
class ModuleManifest:
    name: str
    summary: str
    input: Port
    huri_core: str  # version specifier, e.g. ">=0.3,<0.4"
    output: Optional[Port] = None
    events: List[str] = field(default_factory=list)  # requirement strings
    gpu: bool = False
    actuation: bool = False
    source: str = "<manifest>"


def loads_toml(text: str) -> Dict[str, Any]:
    """tomllib, or tomli on Python 3.10."""

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - python 3.10
        try:
            import tomli as tomllib
        except ImportError as e:
            raise ManifestError(
                "<manifest>", ["reading TOML on Python 3.10 needs `pip install tomli`"]
            ) from e
    decoded: Dict[str, Any] = tomllib.loads(text)
    return decoded


def _port(raw: Any, key: str, problems: List[str]) -> Optional[Port]:
    if not isinstance(raw, Mapping):
        problems.append(f"'{key}' must be a table: {{ topic = ..., event = ... }}")
        return None
    topic, event = raw.get("topic"), raw.get("event")
    extra = set(raw) - {"topic", "event"}
    if extra:
        problems.append(f"'{key}' has unknown keys {sorted(extra)}")
    topic_ok = isinstance(topic, str) and bool(_TOPIC.match(topic))
    event_ok = isinstance(event, str) and bool(_IMPORT_PATH.match(event))
    if not topic_ok:
        problems.append(f"'{key}.topic' must be a namespaced topic, got {topic!r}")
    if not event_ok:
        problems.append(f"'{key}.event' must be 'package.module:Class', got {event!r}")
    if isinstance(topic, str) and isinstance(event, str) and topic_ok and event_ok:
        return Port(topic, event)
    return None


def parse_manifest(
    raw: Mapping[str, Any], source: str = "<manifest>"
) -> ModuleManifest:
    """Validate a decoded manifest; raises ManifestError listing all problems."""

    problems: List[str] = []
    unknown = set(raw) - _KEYS
    if unknown:
        problems.append(f"unknown keys {sorted(unknown)} (allowed: {sorted(_KEYS)})")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        problems.append(f"'name' must be a lowercase identifier, got {name!r}")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("'summary' must be a non-empty string")

    input_port = None
    if "input" in raw:
        input_port = _port(raw["input"], "input", problems)
    else:
        problems.append("'input' is required")
    output_port = _port(raw["output"], "output", problems) if "output" in raw else None

    huri_core = raw.get("huri_core")
    if not isinstance(huri_core, str) or not huri_core.strip():
        problems.append("'huri_core' must be a version range, e.g. '>=0.4,<0.5'")
    else:
        _validate_specifier(huri_core, "huri_core", problems)

    events = raw.get("events", [])
    if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
        problems.append("'events' must be a list of requirement strings")
        events = []
    for requirement in events:
        _validate_requirement(requirement, problems)

    requires = raw.get("requires", {})
    if not isinstance(requires, Mapping):
        problems.append("'requires' must be a table: { gpu = bool, actuation = bool }")
        requires = {}
    for key, value in requires.items():
        if key not in _REQUIRES:
            problems.append(
                f"'requires.{key}' is unknown (allowed: {sorted(_REQUIRES)})"
            )
        elif not isinstance(value, bool):
            problems.append(f"'requires.{key}' must be true or false")

    if problems:
        raise ManifestError(source, problems)
    assert input_port is not None and isinstance(name, str) and isinstance(summary, str)
    assert isinstance(huri_core, str)
    return ModuleManifest(
        name=name,
        summary=summary,
        input=input_port,
        output=output_port,
        huri_core=huri_core,
        events=list(events),
        gpu=bool(requires.get("gpu", False)),
        actuation=bool(requires.get("actuation", False)),
        source=source,
    )


def _validate_specifier(spec: str, key: str, problems: List[str]) -> None:
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
    except ImportError:  # base install: syntax is checked server-side
        return
    try:
        SpecifierSet(spec)
    except InvalidSpecifier:
        problems.append(f"'{key}' is not a valid version range: {spec!r}")


def _validate_requirement(requirement: str, problems: List[str]) -> None:
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        return
    try:
        Requirement(requirement)
    except InvalidRequirement:
        problems.append(f"'events' entry is not a valid requirement: {requirement!r}")


def loads_manifest(text: str, source: str = "<manifest>") -> ModuleManifest:
    try:
        raw = loads_toml(text)
    except ValueError as e:  # tomllib.TOMLDecodeError
        raise ManifestError(source, [f"invalid TOML: {e}"]) from e
    return parse_manifest(raw, source)


def load_manifest(path: Path) -> ModuleManifest:
    return loads_manifest(Path(path).read_text(), str(path))
