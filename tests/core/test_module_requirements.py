"""scripts/module_requirements.py: per-module, per-hardware requirements."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement as PepRequirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "module_requirements", ROOT / "scripts" / "module_requirements.py"
)
assert spec and spec.loader
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)

ALL = sorted(mr.module_packages(ROOT))


def merged(variant, modules=ALL, root=ROOT):
    return mr.merge(mr.collect(list(modules), variant, root))


@pytest.mark.parametrize("variant", mr.VARIANTS)
def test_every_curated_module_installs_together_on_each_variant(variant):
    lines = merged(variant)
    assert not any(line.startswith("huri") for line in lines)  # from packages/


@pytest.mark.parametrize("variant", mr.VARIANTS)
def test_chosen_pins_satisfy_every_module_range(variant):
    reqs = mr.collect(ALL, variant, ROOT)
    pins = {r.name: r.pin for r in reqs if r.pin}
    for req in reqs:
        spec_ = PepRequirement(req.line.split(";")[0]).specifier
        if req.name in pins and spec_:
            assert pins[req.name] in spec_, f"{req.origin}: {req.line}"


def root_names(path):
    names = set()
    for line in (ROOT / path).read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(canonicalize_name(match.group(1)))
    return names


@pytest.mark.parametrize(
    "variant,root_file",
    [("nvidia", "requirements-nvidia.txt"), ("amd", "requirements-amd.txt")],
)
def test_module_files_cover_the_root_hardware_files(variant, root_file):
    """Nothing a root hardware file installs is lost when modules own their pins."""
    covered = {
        canonicalize_name(mr.Requirement(line, "").name) for line in merged(variant)
    }
    assert root_names(root_file) - covered == set()


def test_variant_follows_the_host():
    nvidia = "\n".join(merged("nvidia", ["emo"]))
    amd = "\n".join(merged("amd", ["emo"]))
    assert "transformers==4.51.3" in nvidia and "torch==2.3.1" in nvidia
    assert "transformers==4.46.3" in amd and "torch==" not in amd


def write_package(root, name, modules, deps=(), variants=None):
    path = root / "packages" / name
    path.mkdir(parents=True)
    eps = "\n".join(f'{m} = "x:Y"' for m in modules)
    dep_lines = "".join(f'  "{d}",\n' for d in deps)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\ndependencies = [\n{dep_lines}]\n\n'
        f'[project.entry-points."huri.modules"]\n{eps}\n'
    )
    for variant, text in (variants or {}).items():
        (path / f"requirements-{variant}.txt").write_text(text)


def test_conflicting_pins_name_both_modules(tmp_path):
    write_package(
        tmp_path, "huri-module-a", ["a"], variants={"nvidia": "torch==2.3.1\n"}
    )
    write_package(
        tmp_path, "huri-module-b", ["b"], variants={"nvidia": "torch==2.8.0\n"}
    )
    with pytest.raises(
        mr.ConflictError, match="torch.*huri-module-a.*2.3.1.*huri-module-b.*2.8.0"
    ):
        merged("nvidia", ["a", "b"], tmp_path)
    assert merged("cpu", ["a", "b"], tmp_path) == []  # no cpu pins, no conflict


def test_unknown_module_lists_the_known_ones(tmp_path):
    write_package(tmp_path, "huri-module-a", ["a"])
    with pytest.raises(
        mr.ConflictError, match=r"unknown module\(s\) \['nope'\].*\['a'\]"
    ):
        merged("cpu", ["a", "nope"], tmp_path)


def test_cli_exits_2_with_the_reason(tmp_path, capsys):
    assert mr.main(["--variant", "cpu", "--modules", "nope"]) == 2
    assert "unknown module" in capsys.readouterr().err
    out = tmp_path / "req.txt"
    assert mr.main(["--variant", "amd", "--modules", "rag", "-o", str(out)]) == 0
    assert "transformers==4.46.3" in out.read_text()


def test_parser_without_tomllib_reads_the_same(monkeypatch):
    """Python 3.10 has no tomllib: the fallback parser must agree on every package."""
    expected = {p.name: mr.read_pyproject(p) for p in (ROOT / "packages").iterdir()}
    monkeypatch.setitem(sys.modules, "tomllib", None)
    for name, package in expected.items():
        assert mr.read_pyproject(ROOT / "packages" / name) == package
