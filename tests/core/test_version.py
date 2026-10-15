from pathlib import Path

from packaging.version import Version

import huri_core
from huri_core.core.manifest import loads_toml  # tomli on Python 3.10

ROOT = Path(__file__).resolve().parents[2]


def test_version_is_pep440():
    assert str(Version(huri_core.__version__)) == huri_core.__version__


def test_pyproject_reads_version_from_package():
    project = loads_toml((ROOT / "pyproject.toml").read_text())
    assert project["project"]["name"] == "huri-core"
    assert "version" in project["project"]["dynamic"]
    attr = project["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "huri_core.__version__"
