#!/usr/bin/env python3
"""Merge the requirements of the modules a machine will run, for its hardware.

Each module package (packages/huri-module-*) declares its dependencies in its
pyproject.toml, plus optional exact pins per hardware variant in
requirements-<variant>.txt (cpu, nvidia, amd). Given the modules to install and
the variant, this prints one requirements file — and refuses, naming the
modules involved, when two of them pin the same package to different versions,
since pip alone would only fail late, or silently pick one.

    module_requirements.py --variant nvidia --modules vad,stt,tag,tts_cosyvoice \
        -o .huri-local/requirements-modules.generated.txt

The variant is the host's: the torch build (CUDA, ROCm or CPU) is shared by
every module on the machine. A module without a file for that variant only
needs its pyproject dependencies.

No third-party import, and Python >= 3.10: it runs before the venv exists.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("cpu", "nvidia", "amd")
# Installed from packages/ themselves (editable, --no-deps), never from an index.
INTERNAL = re.compile(r"^huri[-_](core|events|tts|module[-_].*)$")
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PIN = re.compile(r"==\s*([^\s,;#]+)")


class ConflictError(Exception):
    pass


@dataclass
class Package:
    path: Path
    distribution: str
    modules: List[str]
    dependencies: List[str]


@dataclass
class Requirement:
    line: str
    origin: str  # "huri-module-emo (nvidia)"

    @property
    def name(self) -> str:
        match = _NAME.match(self.line)
        assert match, self.line
        return re.sub(r"[-_.]+", "-", match.group(1)).lower()

    @property
    def pin(self) -> Optional[str]:
        spec = self.line.split(";", 1)[0]
        match = _PIN.search(spec)
        return match.group(1) if match else None


def _toml_strings(block: str) -> List[str]:
    return re.findall(r'"([^"]*)"', re.sub(r"(?m)#.*$", "", block))


def read_pyproject(path: Path) -> Package:
    """name, huri.modules entry points and dependencies of a pyproject.toml.

    tomllib when available; otherwise a parser for the simple, flat shape
    the packages in this repo use (Python 3.10 has no tomllib)."""

    text = (path / "pyproject.toml").read_text()
    try:
        import tomllib
    except ImportError:  # Python 3.10
        name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
        deps = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", text)
        eps = re.search(
            r'(?ms)^\[project\.entry-points\."huri\.modules"\]\n(.*?)(?=^\[|\Z)', text
        )
        modules = re.findall(r"(?m)^([A-Za-z0-9_]+)\s*=", eps.group(1)) if eps else []
        return Package(
            path,
            name.group(1) if name else path.name,
            modules,
            _toml_strings(deps.group(1)) if deps else [],
        )
    project = tomllib.loads(text)["project"]
    return Package(
        path,
        project["name"],
        list(project.get("entry-points", {}).get("huri.modules", {})),
        list(project.get("dependencies", [])),
    )


def module_packages(root: Path = ROOT) -> Dict[str, Package]:
    """Every module name provided by packages/*, to its package."""

    found: Dict[str, Package] = {}
    for path in sorted((root / "packages").iterdir()):
        if not (path / "pyproject.toml").is_file():
            continue
        package = read_pyproject(path)
        for module in package.modules:
            if module in found:
                raise ConflictError(
                    f"module {module!r} is provided by both "
                    f"{found[module].distribution} and {package.distribution}"
                )
            found[module] = package
    return found


def variant_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith(("#", "-")):
            lines.append(line)
    return lines


def collect(modules: List[str], variant: str, root: Path = ROOT) -> List[Requirement]:
    available = module_packages(root)
    unknown = [m for m in modules if m not in available]
    if unknown:
        raise ConflictError(
            f"unknown module(s) {unknown}; packages/ provides {sorted(available)}"
        )
    packages: Dict[str, Package] = {}
    for module in modules:
        packages.setdefault(available[module].distribution, available[module])

    requirements: List[Requirement] = []
    for package in packages.values():
        for line in package.dependencies:
            requirements.append(Requirement(line, package.distribution))
        variant_file = package.path / f"requirements-{variant}.txt"
        for line in variant_lines(variant_file):
            requirements.append(
                Requirement(line, f"{package.distribution} ({variant})")
            )
    return [r for r in requirements if not INTERNAL.match(r.name)]


def merge(requirements: List[Requirement]) -> List[str]:
    """One line per package: its exact pin if any declares one, else every
    range. Two different exact pins for one package is a conflict."""

    by_name: Dict[str, List[Requirement]] = {}
    for req in requirements:
        by_name.setdefault(req.name, []).append(req)

    problems: List[str] = []
    lines: List[str] = []
    for name, reqs in sorted(by_name.items()):
        pins = {r.pin for r in reqs if r.pin}
        if len(pins) > 1:
            detail = ", ".join(f"{r.origin} wants {r.line}" for r in reqs if r.pin)
            problems.append(f"{name}: {detail}")
            continue
        origins = sorted({r.origin for r in reqs})
        if pins:
            chosen = next(r.line for r in reqs if r.pin)
        else:
            chosen = sorted({r.line for r in reqs}, key=len)[-1]
        lines.append(f"{chosen}  # {', '.join(origins)}")
    if problems:
        raise ConflictError(
            "these modules cannot be installed together, they pin the same "
            "package to different versions:\n  " + "\n  ".join(problems)
        )
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--modules", required=True, help="comma-separated module names")
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    parser.add_argument("-o", "--output", help="write here instead of stdout")
    args = parser.parse_args(argv)

    modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    try:
        lines = merge(collect(modules, args.variant))
    except ConflictError as e:
        print(f"module_requirements: {e}", file=sys.stderr)
        return 2
    text = (
        f"# Generated by scripts/module_requirements.py for {args.variant}: "
        f"{', '.join(modules)}\n" + "".join(f"{line}\n" for line in lines)
    )
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
