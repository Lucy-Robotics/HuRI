"""Finding modules: the verified registry and the community.

Two sources, merged by module name:
  - the registry of the `huri-modules` repository (registry.toml): the
    curated, verified modules;
  - GitHub repositories tagged `huri-module`, read without installing anything:
    their pyproject.toml (the `huri.modules` entry points) and each module's
    huri_module.toml (summary, supported huri-core range).

Nothing found here is ever imported or loaded: listing is not installing, and
installing is not enabling (HURI_MODULES does that, on the server).
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .core.manifest import ManifestError, loads_manifest, loads_toml

GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
HURI_MODULES_REPO = "Lucy-Robotics/huri-modules"
REGISTRY_PATH = "registry.toml"
# What makes a directory a huri-modules checkout, besides its registry.
CHECKOUT_MARKER = "check_registry.py"
TOPIC = "huri-module"


def modules_ref() -> str:
    """The git ref of huri-modules the default registry is read from and its
    curated packages installed from: one ref for both, so what is installed is
    what the registry describes. HURI_MODULES_REF (a huri-modules release tag,
    e.g. `0.1.0`, or a branch) overrides `main`, its latest release.

    huri-modules has its own versions, so this is not derived from the
    installed huri-core; instead `list` and `install` check each module's
    huri_core range against the installed huri-core."""

    return os.environ.get("HURI_MODULES_REF") or "main"


def default_registry() -> str:
    return RAW.format(repo=HURI_MODULES_REPO, ref=modules_ref(), path=REGISTRY_PATH)


# The registry of huri-modules at one ref, on GitHub.
_HURI_MODULES_REGISTRY = re.compile(
    re.escape(RAW.format(repo=HURI_MODULES_REPO, ref="@", path=REGISTRY_PATH))
    .replace("@", "(?P<ref>.+)")
    .join("^$")
)


def curated_install(path: str, ref: Optional[str] = None) -> str:
    """pip target of a curated package, a directory of huri-modules at `ref`
    (default: modules_ref())."""
    return (
        f"git+https://github.com/{HURI_MODULES_REPO}.git@{ref or modules_ref()}"
        f"#subdirectory={path}"
    )


# url -> text, or None when the resource does not exist (HTTP 404).
Fetch = Callable[[str], Optional[str]]
# Where notes and warnings go (stderr), apart from the command's output.
Warn = Callable[[str], None]


class HubError(RuntimeError):
    pass


def warn_stderr(message: str) -> None:
    print(f"huri hub: {message}", file=sys.stderr)


def http_fetch(url: str) -> Optional[str]:
    headers = {"User-Agent": "huri-hub", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(GITHUB_API):
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            body: bytes = response.read()
            return body.decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise HubError(f"{url}: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise HubError(f"{url}: {e.reason}") from e


@dataclass
class HubModule:
    name: str
    summary: str
    huri_core: Optional[str]  # supported range, None when unknown
    source: str  # where it comes from, for humans
    install: str  # what to give pip
    verified: bool
    # A curated package of huri-modules: its directory there ("packages/...").
    curated_path: Optional[str] = None

    def supports(self, core_version: str) -> Optional[bool]:
        """Whether this module supports that huri-core; None if unknown."""
        if not self.huri_core:
            return None
        from packaging.specifiers import SpecifierSet

        return SpecifierSet(self.huri_core).contains(core_version, prereleases=True)


# --- the verified registry ------------------------------------------------------

# The `source` of a registry entry: kind -> (required keys, optional keys).
# The same schema as huri-modules' check_registry.py, which CI runs on every
# change of the registry; the hub checks it again rather than install from a
# source that checker would refuse.
SOURCE_KEYS = {
    "path": ({"path"}, set()),
    "git": ({"git", "tag", "submodule"}, {"subdirectory"}),
}


def find_local_registry(start: Path) -> Optional[Path]:
    """registry.toml of the huri-modules checkout `start` is in, if any."""
    for directory in [start, *start.parents]:
        candidate = directory / REGISTRY_PATH
        if candidate.is_file() and (directory / CHECKOUT_MARKER).is_file():
            return candidate
    return None


def _check_source(where: str, source: Any) -> str:
    """The kind of a well-formed `source` table; HubError otherwise."""
    if not isinstance(source, dict):
        raise HubError(f"{where} has no `path` or `git` source")
    kinds = [kind for kind in SOURCE_KEYS if kind in source]
    if not kinds:
        raise HubError(f"{where} has no `path` or `git` source")
    if len(kinds) > 1:
        raise HubError(f"{where}: a source is either `path` or `git`, not both")
    kind = kinds[0]
    required, optional = SOURCE_KEYS[kind]
    if kind == "git" and not source.get("tag"):
        raise HubError(f"{where}: a git source must pin a `tag`")
    missing = sorted(required - set(source))
    unknown = sorted(set(source) - required - optional)
    if missing or unknown:
        raise HubError(
            f"{where}: {kind} source"
            + (f" lacks {missing}" if missing else "")
            + (f" has unknown {unknown}" if unknown else "")
        )
    if not all(isinstance(value, str) and value for value in source.values()):
        raise HubError(f"{where}: every `source` value must be a non-empty string")
    if kind == "git" and not source["git"].startswith("https://"):
        raise HubError(f"{where}: a git source must be an https URL")
    return kind


@dataclass
class Registry:
    """A registry.toml, and where its curated packages are installed from."""

    location: str
    # huri-modules ref the curated packages not found in `checkout` come from.
    ref: str
    # Directory the registry was read from (a huri-modules checkout, or any
    # directory holding a registry.toml); None for a URL.
    checkout: Optional[Path]
    modules: List[HubModule]
    # Directories of the curated event packages ([[events]], "packages/...").
    events: List[str]

    def target(self, path: str) -> str:
        """pip target of a curated package: the checkout's directory when it
        has it, otherwise huri-modules at `ref`."""
        if self.checkout is not None and (self.checkout / path).is_dir():
            return str(self.checkout / path)
        return curated_install(path, self.ref)

    def read(self, path: str, fetch: Fetch) -> Optional[str]:
        """A file of huri-modules, from where `target` installs it."""
        if self.checkout is not None and (self.checkout / path).is_file():
            return (self.checkout / path).read_text()
        if self.checkout is not None and (self.checkout / path).parent.is_dir():
            return None  # that package is in the checkout, without this file
        return fetch(RAW.format(repo=HURI_MODULES_REPO, ref=self.ref, path=path))

    def companions(self, module: HubModule, fetch: Fetch) -> List[str]:
        """pip targets to install along with `module`.

        Modules depend on the curated packages of huri-modules by bare name
        (`huri-events>=0.1,<0.2`), and those are on no package index: they come
        from huri-modules, at the same ref as the module, in the same pip
        command, so pip never looks them up anywhere else (an index would let
        anyone publish a package of that name). Always the event packages
        ([[events]]); for a curated module also the other curated libraries it
        depends on (e.g. huri-tts), read from its pyproject.toml: a curated
        library is the directory packages/<its distribution name>.
        """
        paths = list(self.events)
        if module.curated_path is not None:
            paths += _curated_dependencies(module.curated_path, self, fetch)
        seen = {module.curated_path}
        targets = []
        for path in paths:
            if path not in seen:
                seen.add(path)
                targets.append(self.target(path))
        return targets


def _curated_dependencies(path: str, registry: Registry, fetch: Fetch) -> List[str]:
    """Directories of the curated libraries the package at `path` needs,
    directly or not (huri-core excluded: it is installed already)."""
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    found: List[str] = []
    queue, seen = [path], {path}
    while queue:
        text = registry.read(f"{queue.pop(0)}/pyproject.toml", fetch)
        if text is None:
            continue
        try:
            requires = loads_toml(text).get("project", {}).get("dependencies", [])
        except ValueError:  # malformed TOML: pip will say so
            continue
        for requirement in requires:
            try:
                name = canonicalize_name(Requirement(requirement).name)
            except InvalidRequirement:
                continue
            directory = f"packages/{name}"
            if not name.startswith("huri-") or name == "huri-core":
                continue
            if directory in seen:
                continue
            seen.add(directory)
            if registry.read(f"{directory}/pyproject.toml", fetch) is not None:
                found.append(directory)
                queue.append(directory)
    return found


def _install_target(
    name: str, source: Mapping[str, Any], checkout: Optional[Path], ref: str
) -> Tuple[str, str]:
    """(pip target, where it comes from) of a registry entry's `source`.

    path = "packages/<pkg>": a curated package of huri-modules. From a local
        checkout that has it, that directory; otherwise the huri-modules
        repository at `ref`.
    git = "<url>", tag = "<tag>" (subdirectory = "<dir>" optional): a verified
        third-party module, always installed at the reviewed tag. Its pinned
        submodule (`submodule = "modules/<name>"`) is for huri-modules' own CI
        and review, not needed here.
    """

    kind = _check_source(f"registry entry {name!r}", source)
    if kind == "path":
        path = str(source["path"])
        if checkout is not None and (checkout / path).is_dir():
            return str(checkout / path), path
        return curated_install(path, ref), path
    target = f"git+{source['git']}@{source['tag']}"
    if source.get("subdirectory"):
        target += f"#subdirectory={source['subdirectory']}"
    return target, f"{source['git']}@{source['tag']}"


def _curated_ref(location: str, is_url: bool, local: bool, warn: Warn) -> str:
    """The huri-modules ref curated packages are installed from, for a registry
    read at `location`: the ref of that registry when it is huri-modules' own
    on GitHub, otherwise modules_ref() (with a note, since nothing ties that
    registry to that ref)."""
    match = _HURI_MODULES_REGISTRY.match(location) if is_url else None
    override = os.environ.get("HURI_MODULES_REF")
    if match:
        ref = match.group("ref")
        if override and override != ref:
            warn(
                f"warning: HURI_MODULES_REF={override} ignored, the registry read "
                f"is huri-modules@{ref}: its curated packages come from {ref} too"
            )
        return ref
    if is_url or not local:
        warn(
            f"note: {location} is not huri-modules' own registry; curated packages "
            f"missing from it are installed from huri-modules@{modules_ref()} "
            "(HURI_MODULES_REF)"
        )
    return modules_ref()


def read_registry(
    location: str, fetch: Fetch = http_fetch, warn: Warn = warn_stderr
) -> Registry:
    """The verified registry, from a registry.toml path or URL, or a
    huri-modules checkout directory."""
    is_url = location.startswith(("http://", "https://"))
    if not is_url and Path(location).is_dir():
        location = str(Path(location) / REGISTRY_PATH)
    text: Optional[str] = None
    if is_url:
        text = fetch(location)
    elif Path(location).is_file():
        text = Path(location).read_text()
    if text is None:
        raise HubError(f"no registry at {location}")
    try:
        registry = loads_toml(text)
    except ValueError as e:  # tomllib.TOMLDecodeError
        raise HubError(f"{location} is not valid TOML: {e}") from e
    if registry.get("schema", 1) != 1:
        raise HubError(
            f"{location} has registry schema {registry.get('schema')!r}; this "
            "huri-core reads schema 1: upgrade huri-core, or use a huri-modules "
            "release built for it (HURI_MODULES_REF)"
        )
    checkout = None if is_url else Path(location).resolve().parent
    local = checkout is not None and (checkout / CHECKOUT_MARKER).is_file()
    ref = _curated_ref(location, is_url, local, warn)

    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    events = []
    for i, entry in enumerate(registry.get("events", [])):
        package = entry.get("package") if isinstance(entry, dict) else None
        where = f"registry events {package or f'#{i + 1}'!r}"
        source: Any = entry.get("source") if isinstance(entry, dict) else None
        if _check_source(where, source) != "path":
            raise HubError(f"{where}: event packages are curated, a `path` source")
        events.append(str(source["path"]))

    modules = []
    for i, entry in enumerate(registry.get("module", [])):
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise HubError(f"registry module #{i + 1} has no `name`")
        huri_core = entry.get("huri_core")
        if huri_core is not None:
            try:
                SpecifierSet(str(huri_core))
            except InvalidSpecifier as e:
                raise HubError(
                    f"registry entry {name!r}: invalid huri_core range {huri_core!r}"
                ) from e
        source = entry.get("source", {})
        install, where = _install_target(name, source, checkout, ref)
        modules.append(
            HubModule(
                name=name,
                summary=str(entry.get("summary", "")),
                huri_core=huri_core,
                source=where,
                install=install,
                verified=True,
                curated_path=str(source["path"]) if "path" in source else None,
            )
        )
    return Registry(location, ref, checkout, modules, events)


def load_registry(
    location: str, fetch: Fetch = http_fetch, warn: Warn = warn_stderr
) -> List[HubModule]:
    """The verified modules of a registry (see read_registry)."""
    return read_registry(location, fetch, warn).modules


# --- the community (GitHub topic) ---------------------------------------------


def search(query: str = "", fetch: Fetch = http_fetch) -> List[Dict[str, Any]]:
    """Repositories tagged `huri-module` matching `query` (only listed)."""
    q = urllib.parse.quote(f"topic:{TOPIC} {query}".strip())
    text = fetch(f"{GITHUB_API}/search/repositories?q={q}&per_page=50")
    if text is None:
        return []
    return [
        {
            "repo": item["full_name"],
            "description": item.get("description") or "",
            "url": item["html_url"],
            "ref": item.get("default_branch", "main"),
        }
        for item in json.loads(text).get("items", [])
    ]


def read_repository(repo: Dict[str, Any], fetch: Fetch = http_fetch) -> List[HubModule]:
    """The modules a repository provides, from its pyproject.toml and manifests."""

    def raw(path: str) -> Optional[str]:
        return fetch(RAW.format(repo=repo["repo"], ref=repo["ref"], path=path))

    pyproject = raw("pyproject.toml")
    if pyproject is None:
        return []
    try:
        project = loads_toml(pyproject).get("project", {})
    except ValueError:  # malformed TOML
        return []
    entry_points = project.get("entry-points", {}).get("huri.modules", {})

    modules = []
    for name, target in entry_points.items():
        package = target.split(":", 1)[0].split(".", 1)[0]
        manifest = None
        for path in (
            f"{package}/huri_module.toml",
            f"{package}/manifests/{name}/huri_module.toml",
            "huri_module.toml",
        ):
            text = raw(path)
            if text is None:
                continue
            try:
                candidate = loads_manifest(text, f"{repo['repo']}/{path}")
            except ManifestError:
                continue
            if candidate.name == name:
                manifest = candidate
                break
        modules.append(
            HubModule(
                name=name,
                summary=manifest.summary if manifest else repo.get("description", ""),
                huri_core=manifest.huri_core if manifest else None,
                source=repo["url"],
                install=f"git+{repo['url']}.git",
                verified=False,
            )
        )
    return modules


def catalog(registry: List[HubModule], community: List[HubModule]) -> List[HubModule]:
    """Both sources by name; a verified entry wins over a community one."""
    merged: Dict[str, HubModule] = {m.name: m for m in community}
    merged.update({m.name: m for m in registry})
    return sorted(merged.values(), key=lambda m: m.name)


# --- the CLI ----------------------------------------------------------------------


def installed_core_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("huri-core")
    except PackageNotFoundError:
        from huri_core import __version__

        return __version__


def _registry_location(option: Optional[str], warn: Warn = warn_stderr) -> str:
    """--registry, else HURI_REGISTRY, else the huri-modules checkout the
    current directory is in, else huri-modules on GitHub at modules_ref()."""
    if os.environ.get("HURI_REF") and not os.environ.get("HURI_MODULES_REF"):
        warn(
            "warning: HURI_REF is ignored: it was renamed HURI_MODULES_REF, and is "
            "now a ref of huri-modules (e.g. 0.1.0), not of huri-core"
        )
    if option:
        return option
    if os.environ.get("HURI_REGISTRY"):
        return os.environ["HURI_REGISTRY"]
    local = find_local_registry(Path.cwd())
    if local is None:
        return default_registry()
    if os.environ.get("HURI_MODULES_REF"):
        warn(
            f"warning: HURI_MODULES_REF={os.environ['HURI_MODULES_REF']} ignored: "
            f"using the huri-modules checkout at {local.parent} (pass "
            f"--registry {default_registry()} to use that ref)"
        )
    return str(local)


def _read_registry(args: Any, fetch: Fetch) -> Registry:
    registry = read_registry(_registry_location(args.registry), fetch)
    warn_stderr(f"registry: {registry.location}")
    return registry


def cmd_search(args: Any, fetch: Fetch, out: Callable[[str], None]) -> int:
    repos = search(" ".join(args.query), fetch)
    for repo in repos:
        out(f"{repo['repo']:40s} {repo['description']}")
    out(f"{len(repos)} repositories tagged {TOPIC!r}")
    return 0


def cmd_list(args: Any, fetch: Fetch, out: Callable[[str], None]) -> int:
    core = args.core or installed_core_version()
    from packaging.version import InvalidVersion, Version

    try:
        Version(core)
    except InvalidVersion as e:
        raise HubError(f"invalid huri-core version {core!r}") from e
    registry = _read_registry(args, fetch).modules
    community: List[HubModule] = []
    if not args.verified and not args.no_crawl:
        for repo in search("", fetch):
            community += read_repository(repo, fetch)
    shown = 0
    for module in catalog(registry, community):
        if args.verified and not module.verified:
            continue
        supported = module.supports(core)
        if supported is False and not args.all:
            continue
        mark = "verified " if module.verified else "community"
        range_ = module.huri_core or "huri-core range unknown"
        out(f"{module.name:16s} {mark} {range_:24s} {module.summary}")
        shown += 1
    out(f"{shown} module(s) for huri-core {core}")
    return 0


def cmd_install(args: Any, fetch: Fetch, out: Callable[[str], None]) -> int:
    target, name = args.module, None
    targets = [target]
    if not re.match(r"^(git\+|https?://|\.|/)", target):
        name = target
        registry = _read_registry(args, fetch)
        known = {m.name: m for m in registry.modules}
        if name in known:
            module = known[name]
            out(f"{name}: verified, from {module.source}")
        else:
            # Anyone can tag a repository and claim a name: never install an
            # unreviewed one implicitly, nor pick one of several claimants.
            candidates = [
                m
                for repo in search(name, fetch)
                for m in read_repository(repo, fetch)
                if m.name == name
            ]
            if not candidates:
                raise HubError(f"no module named {name!r} in the registry or on GitHub")
            if len(candidates) > 1:
                listed = "\n".join(f"  {m.install}" for m in candidates)
                raise HubError(
                    f"{len(candidates)} community repositories provide {name!r}; "
                    f"install one explicitly:\n{listed}"
                )
            module = candidates[0]
            if not args.community:
                raise HubError(
                    f"{name!r} is a community module, not reviewed ({module.source}). "
                    "Read its code first; install it with --community, or by URL: "
                    f"huri hub install {module.install}"
                )
            out(f"{name}: community (not reviewed), from {module.source}")
        core = installed_core_version()
        if module.supports(core) is False:
            # pip would otherwise "fix" it by replacing the installed huri-core.
            hint = (
                "Use the huri-modules release built for it (HURI_MODULES_REF="
                f"<tag>, see https://github.com/{HURI_MODULES_REPO}#readme)"
                if module.verified
                else "Use a release of it built for that huri-core"
            )
            raise HubError(
                f"{name!r} supports huri-core {module.huri_core}, but {core} is "
                f"installed. {hint}, or install it by URL."
            )
        targets = [module.install, *registry.companions(module, fetch)]
    command = [sys.executable, "-m", "pip", "install", *targets]
    out("$ " + " ".join(command))
    if not args.dry_run:
        import subprocess

        code = subprocess.call(command)
        if code != 0:
            return code
    label = name or "<module name>"
    out(
        "Installed, not enabled. To use it, add it to the server's HURI_MODULES and "
        f"to the client config:\n  modules:\n    {label}:\n      name: {label}"
    )
    return 0


def main(argv: Optional[List[str]] = None, fetch: Fetch = http_fetch) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="huri hub", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="GitHub repositories tagged huri-module")
    p.add_argument("query", nargs="*")
    p.set_defaults(run=cmd_search)

    p = sub.add_parser("list", help="modules supporting a huri-core version")
    p.add_argument("--core", help="huri-core version (default: the installed one)")
    p.add_argument("--verified", action="store_true", help="the registry only")
    p.add_argument("--all", action="store_true", help="also unsupported versions")
    p.add_argument("--no-crawl", action="store_true", help="skip GitHub")
    p.add_argument(
        "--registry", help="registry.toml path or URL, or a huri-modules checkout"
    )
    p.set_defaults(run=cmd_list)

    p = sub.add_parser("install", help="pip-install a module (does not enable it)")
    p.add_argument("module", help="a module name, or a pip target (git+https://...)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--community",
        action="store_true",
        help="allow a module that is not in the verified registry",
    )
    p.add_argument(
        "--registry", help="registry.toml path or URL, or a huri-modules checkout"
    )
    p.set_defaults(run=cmd_install)

    args = parser.parse_args(argv)
    try:
        code: int = args.run(args, fetch, print)
        return code
    except HubError as e:
        print(f"huri hub: {e}", file=sys.stderr)
        return 1
    except ImportError as e:  # packaging / tomli: the [client] extra
        print(
            f"huri hub: {e}. It needs huri-core's client extra: "
            'pip install "huri-core[client]"',
            file=sys.stderr,
        )
        return 1
