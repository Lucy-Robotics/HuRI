"""Discovery of modules and events through Python entry points.

A package registers what it provides in its pyproject.toml:

    [project.entry-points."huri.modules"]
    stt = "huri_stt.module:STT"

    [project.entry-points."huri.events"]
    "huri.transcript" = "huri_events.speech:Transcript"

so installing it with pip is all it takes for HuRI to find it. A module can also
be referenced by import path (`my_pkg.servo:Servo`) without any packaging, as
long as the server's HURI_MODULES lists that path.

Event topics are namespaced, `<namespace>.<name>`. `huri.` is reserved for the
curated events; every other distribution registers its topics under one
namespace of its own (`acme.servo_angles`), so a third-party event can replace
a curated one without colliding with it.
"""

import importlib
import importlib.util
import re
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Type

from .events import EventData
from .manifest import MANIFEST_FILE, ModuleManifest, loads_manifest
from .module import Module

MODULES_GROUP = "huri.modules"
EVENTS_GROUP = "huri.events"

CURATED_NAMESPACE = "huri"
# The only distribution allowed to register `huri.*` topics.
CURATED_DISTRIBUTIONS = frozenset({"huri-events"})
_TOPIC = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")


class PluginError(RuntimeError):
    """A plugin is missing, broken, or conflicts with another one."""


def _dist(ep: EntryPoint) -> str:
    dist = getattr(ep, "dist", None)
    return dist.name if dist is not None else "<unknown distribution>"


def _normalize(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist).lower()


def topic_namespace(topic: str) -> str:
    return topic.split(".", 1)[0]


def _check_namespaces(eps: Iterable[EntryPoint]) -> None:
    """One namespace per distribution, one distribution per namespace, and
    `huri.` only for the curated events."""

    owner: Dict[str, str] = {}  # namespace -> distribution
    claimed: Dict[str, str] = {}  # distribution -> namespace
    for ep in eps:
        topic, dist = ep.name, _dist(ep)
        if not _TOPIC.match(topic):
            raise PluginError(
                f"event topic {topic!r} (from {dist}) must be namespaced, "
                "'<namespace>.<name>' in lowercase, e.g. 'acme.servo_angles'"
            )
        namespace = topic_namespace(topic)
        if (
            namespace == CURATED_NAMESPACE
            and _normalize(dist) not in CURATED_DISTRIBUTIONS
        ):
            raise PluginError(
                f"{dist} registers {topic!r}, but the {CURATED_NAMESPACE!r} "
                "namespace is reserved for the curated events. Register it under "
                f"your own namespace instead, e.g. "
                f"'{_normalize(dist).replace('-', '_')}.{topic.split('.', 1)[1]}'"
            )
        if dist in claimed and claimed[dist] != namespace:
            raise PluginError(
                f"{dist} registers topics in two namespaces, {claimed[dist]!r} and "
                f"{namespace!r} ({topic!r}): a package claims a single namespace"
            )
        if namespace in owner and owner[namespace] != dist:
            raise PluginError(
                f"namespace {namespace!r} is claimed by both {owner[namespace]} and "
                f"{dist} ({topic!r}). Uninstall one of them."
            )
        claimed[dist], owner[namespace] = namespace, dist


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
            "group: huri-core ships none. pip install a module package (the "
            "curated ones: https://github.com/Lucy-Robotics/huri-modules, or "
            "`huri hub install <name>` with huri-hub, from huri-modules)."
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

    found = _entry_points(EVENTS_GROUP)
    _check_namespaces(found.values())
    events: Dict[str, Type[EventData]] = {}
    for topic, ep in found.items():
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


# --- manifests (huri_module.toml) ----------------------------------------------


def _package_dirs(module_path: str) -> Iterator[Path]:
    """Directories of a module's top-level package, found without importing it."""

    top = module_path.split(".", 1)[0]
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return
    if spec is None:
        return
    for location in spec.submodule_search_locations or []:
        yield Path(location)
    if spec.origin and not spec.submodule_search_locations:
        yield Path(spec.origin).parent


def _manifest_files(ep: Optional[EntryPoint], module_path: str) -> Iterator[Path]:
    seen = set()
    dist = getattr(ep, "dist", None) if ep is not None else None
    files = (dist.files or []) if dist is not None else []
    for file in files:
        if dist is not None and file.name == MANIFEST_FILE:
            path = Path(str(dist.locate_file(file)))
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path
    # Editable installs and import paths: the files are next to the code.
    for directory in _package_dirs(module_path):
        for path in sorted(directory.rglob(MANIFEST_FILE)):
            if path not in seen:
                seen.add(path)
                yield path


def _module_dir(module_path: str) -> Optional[Path]:
    """Directory holding a module's file (imports its parent packages, as
    loading the module does anyway)."""

    try:
        spec = importlib.util.find_spec(module_path)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent


def find_manifest(name: str) -> Optional[ModuleManifest]:
    """The manifest of an installed module (by name) or of an import path.
    None when the module ships none.

    An import path that is the target of an installed entry point is that
    module: its manifest is found as for its name. Any other import path only
    looks next to the module's own file (`huri_module.toml`, or
    `manifests/*/huri_module.toml`), for a manifest named after the class."""

    if is_import_path(name):
        installed = [
            ep for ep in _entry_points(MODULES_GROUP).values() if ep.value == name
        ]
        if installed:
            return find_manifest(installed[0].name)
        module_path, attr = name.split(":", 1)
        directory = _module_dir(module_path)
        if directory is None:
            return None
        wanted = attr.split(".")[-1].lower()
        candidates = [directory / MANIFEST_FILE]
        candidates += sorted(directory.glob(f"manifests/*/{MANIFEST_FILE}"))
        for path in candidates:
            if path.is_file():
                manifest = loads_manifest(path.read_text(), str(path))
                if manifest.name == wanted:
                    return manifest
        return None

    ep = _entry_points(MODULES_GROUP).get(name)
    if ep is None:
        return None
    module_path = ep.value.split(":", 1)[0]
    for path in _manifest_files(ep, module_path):
        manifest = loads_manifest(path.read_text(), str(path))
        if manifest.name == name:
            return manifest
    return None
