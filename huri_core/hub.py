"""Finding modules: the verified registry and the community, per huri-core version.

Two sources, merged by module name:
  - the `huri-modules` registry (registry.toml): the curated, verified modules;
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
from typing import Any, Callable, Dict, List, Optional

from .core.manifest import ManifestError, loads_manifest, loads_toml

GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
HURI_REPO = "Lucy-Robotics/HuRI"
REGISTRY_PATH = "huri-modules/registry.toml"
TOPIC = "huri-module"


def huri_ref() -> str:
    """The git ref of the HuRI repository the registry is read from and its
    modules installed from — one ref for both, so what is installed is what
    the registry describes. HURI_REF overrides; otherwise the release tag of
    the installed huri-core, or `dev` for a development build."""

    if os.environ.get("HURI_REF"):
        return os.environ["HURI_REF"]
    version = installed_core_version()
    return "dev" if "dev" in version or "+" in version else version


def default_registry() -> str:
    return RAW.format(repo=HURI_REPO, ref=huri_ref(), path=REGISTRY_PATH)


# url -> text, or None when the resource does not exist (HTTP 404).
Fetch = Callable[[str], Optional[str]]


class HubError(RuntimeError):
    pass


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

    def supports(self, core_version: str) -> Optional[bool]:
        """Whether this module supports that huri-core; None if unknown."""
        if not self.huri_core:
            return None
        from packaging.specifiers import SpecifierSet

        return SpecifierSet(self.huri_core).contains(core_version, prereleases=True)


# --- the verified registry ------------------------------------------------------


def find_local_registry(start: Path) -> Optional[Path]:
    """registry.toml of the HuRI checkout `start` is in, if any."""
    for directory in [start, *start.parents]:
        candidate = directory / "huri-modules" / "registry.toml"
        if candidate.is_file():
            return candidate
    return None


def load_registry(location: str, fetch: Fetch = http_fetch) -> List[HubModule]:
    """The verified modules, from a registry.toml path or URL."""
    is_url = location.startswith(("http://", "https://"))
    text = fetch(location) if is_url else Path(location).read_text()
    if text is None:
        raise HubError(f"no registry at {location}")
    registry = loads_toml(text)
    checkout = None if is_url else Path(location).resolve().parents[1]

    modules = []
    for entry in registry.get("module", []):
        source = entry.get("source", {})
        if "path" in source:
            if checkout is not None:
                install = str(checkout / source["path"])
            else:
                install = (
                    f"git+https://github.com/{HURI_REPO}.git@{huri_ref()}"
                    f"#subdirectory={source['path']}"
                )
            where = source["path"]
        elif "git" in source:
            ref = f"@{source['tag']}" if "tag" in source else ""
            install = f"git+{source['git']}{ref}"
            where = source["git"]
        else:
            install, where = entry.get("package", entry["name"]), "?"
        modules.append(
            HubModule(
                name=entry["name"],
                summary=entry.get("summary", ""),
                huri_core=entry.get("huri_core"),
                source=where,
                install=install,
                verified=True,
            )
        )
    return modules


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


def _registry_location(option: Optional[str]) -> str:
    if option:
        return option
    if os.environ.get("HURI_REGISTRY"):
        return os.environ["HURI_REGISTRY"]
    local = find_local_registry(Path.cwd())
    return str(local) if local else default_registry()


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
    registry = load_registry(_registry_location(args.registry), fetch)
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
    if not re.match(r"^(git\+|https?://|\.|/)", target):
        name = target
        known = {
            m.name: m for m in load_registry(_registry_location(args.registry), fetch)
        }
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
        target = module.install
    command = [sys.executable, "-m", "pip", "install", target]
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
    p.add_argument("--registry", help="registry.toml path or URL")
    p.set_defaults(run=cmd_list)

    p = sub.add_parser("install", help="pip-install a module (does not enable it)")
    p.add_argument("module", help="a module name, or a pip target (git+https://...)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--community",
        action="store_true",
        help="allow a module that is not in the verified registry",
    )
    p.add_argument("--registry", help="registry.toml path or URL")
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
