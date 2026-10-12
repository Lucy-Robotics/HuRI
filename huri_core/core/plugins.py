"""Discovery of modules and events through Python entry points.

A package registers what it provides in its pyproject.toml:

    [project.entry-points."huri.modules"]
    stt = "huri_stt.module:STT"

    [project.entry-points."huri.events"]
    "transcript" = "huri_events.text:Transcript"

so installing it with pip is all it takes for HuRI to find it. A module can also
be referenced by import path (`my_pkg.servo:Servo`) without any packaging, as
long as the server's HURI_MODULES lists that path.
"""

import importlib
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Dict, Iterable, List, Optional, Type

from .events import EventData
from .module import Module

MODULES_GROUP = "huri.modules"
EVENTS_GROUP = "huri.events"


class PluginError(RuntimeError):
    """A plugin is missing, broken, or conflicts with another one."""


def _dist(ep: EntryPoint) -> str:
    dist = getattr(ep, "dist", None)
    return dist.name if dist is not None else "<unknown distribution>"


def _entry_points(group: str) -> Dict[str, EntryPoint]:
    """Entry points of a group by name. The same name registered by two
    distributions with different targets is an error, not a silent override."""

    found: Dict[str, EntryPoint] = {}
    for ep in entry_points(group=group):
        other = found.get(ep.name)
        if other is not None and other.value != ep.value:
            raise PluginError(
                f"{group} entry point {ep.name!r} is registered twice: "
                f"{other.value} (from {_dist(other)}) and {ep.value} "
                f"(from {_dist(ep)}). Uninstall one of them."
            )
        found[ep.name] = ep
    return found


def is_import_path(name: str) -> bool:
    return ":" in name


def import_object(path: str) -> Any:
    """Resolve `package.module:Attribute` (tier 0, no packaging needed)."""

    module_path, _, attr_path = path.partition(":")
    if not module_path or not attr_path:
        raise PluginError(
            f"invalid import path {path!r}, expected 'package.module:Name'"
        )
    try:
        obj: Any = importlib.import_module(module_path)
    except ImportError as e:
        raise PluginError(f"cannot import {module_path!r} for {path!r}: {e}") from e
    for attr in attr_path.split("."):
        try:
            obj = getattr(obj, attr)
        except AttributeError as e:
            raise PluginError(f"{module_path!r} has no attribute {attr_path!r}") from e
    return obj


def _module_class(obj: Any, origin: str) -> Type[Module]:
    if not (isinstance(obj, type) and issubclass(obj, Module)):
        raise PluginError(f"{origin} does not point to a Module subclass: {obj!r}")
    return obj


def parse_selection(selection: Optional[str]) -> List[str]:
    """Split a HURI_MODULES value ("stt,rag,my_pkg.servo:Servo")."""

    if not selection:
        return []
    return [name.strip() for name in selection.split(",") if name.strip()]


def installed_modules() -> List[str]:
    """Names of every installed module, without importing any of them."""

    return sorted(_entry_points(MODULES_GROUP))


def load_modules(selection: Optional[Iterable[str]] = None) -> Dict[str, Type[Module]]:
    """Load the modules this server registers.

    :selection: module names and/or import paths to load (the HURI_MODULES
        allow-list). Default-deny: None or empty loads no module at all, since
        any installed package can register one and the operator must opt each
        in. Only the selected entry points are imported, so a host never
        imports a module it does not run (and its heavy dependencies).
    """

    wanted = list(selection or [])
    if not wanted:
        return {}

    available = _entry_points(MODULES_GROUP)
    unknown = [n for n in wanted if not is_import_path(n) and n not in available]
    if unknown:
        hint = (
            f"Installed modules: {sorted(available)}"
            if available
            else f"No module is installed under the {MODULES_GROUP!r} entry point "
            "group: install huri-core (`pip install -e .` from a checkout) or a "
            "module package."
        )
        raise ValueError(
            f"HURI_MODULES lists unknown module(s): {sorted(unknown)}. {hint}"
        )

    modules: Dict[str, Type[Module]] = {}
    for name in wanted:
        if is_import_path(name):
            modules[name] = _module_class(import_object(name), name)
            continue
        ep = available[name]
        try:
            obj = ep.load()
        except Exception as e:
            raise PluginError(
                f"module {name!r} ({ep.value}, from {_dist(ep)}) failed to import: "
                f"{type(e).__name__}: {e}"
            ) from e
        modules[name] = _module_class(obj, f"module {name!r} ({ep.value})")
    return modules


def load_events() -> Dict[str, Type[EventData]]:
    """Every installed event class, by topic."""

    events: Dict[str, Type[EventData]] = {}
    for topic, ep in _entry_points(EVENTS_GROUP).items():
        try:
            obj = ep.load()
        except Exception as e:
            raise PluginError(
                f"event {topic!r} ({ep.value}, from {_dist(ep)}) failed to import: "
                f"{type(e).__name__}: {e}"
            ) from e
        if not (isinstance(obj, type) and issubclass(obj, EventData)):
            raise PluginError(
                f"event {topic!r} ({ep.value}, from {_dist(ep)}) is not an "
                f"EventData subclass: {obj!r}"
            )
        events[topic] = obj
    return events
