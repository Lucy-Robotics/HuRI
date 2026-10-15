"""The `huri` command dispatches to the "huri.commands" entry points
(huri_core.cli); core itself provides none."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from huri_core import cli
from huri_core.core import plugins
from huri_core.core.manifest import loads_toml  # tomli on Python 3.10


class FakeEntryPoint:
    def __init__(self, name, target, dist="pkg-a", value=None):
        self.name = name
        self.value = value or f"{dist.replace('-', '_')}.cli:{name}"
        self.dist = SimpleNamespace(name=dist)
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


@pytest.fixture
def installed(monkeypatch):
    """Replace the installed "huri.commands" entry points with a fake set."""

    registry = {cli.COMMANDS_GROUP: []}
    monkeypatch.setattr(plugins, "entry_points", lambda group: registry[group])
    return registry[cli.COMMANDS_GROUP]


def greet(argv):
    """Say hello to whoever is named."""
    print("hello", *argv)
    return 0


def test_a_command_gets_the_remaining_arguments(installed, capsys):
    installed.append(FakeEntryPoint("greet", greet))
    assert cli.main(["greet", "ada", "--loud"]) == 0
    assert capsys.readouterr().out == "hello ada --loud\n"


def test_the_exit_code_is_the_commands(installed):
    installed.append(FakeEntryPoint("fail", lambda argv: 3))
    installed.append(FakeEntryPoint("quiet", lambda argv: None))
    assert cli.main(["fail"]) == 3
    assert cli.main(["quiet"]) == 0


def test_help_lists_installed_commands(installed, capsys):
    installed.append(FakeEntryPoint("greet", greet))
    for flag in ("-h", "--help"):
        assert cli.main([flag]) == 0
        out = capsys.readouterr().out
        assert "greet  Say hello to whoever is named." in out
        assert cli.COMMANDS_GROUP in out


def test_no_command_prints_usage_and_is_a_usage_error(installed, capsys):
    installed.append(FakeEntryPoint("greet", greet))
    assert cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: huri" in captured.err and "greet" in captured.err


def test_nothing_installed_says_where_commands_come_from(installed, capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "no command is installed" in out
    assert "huri-hub" in out


def test_unknown_command_names_the_installed_ones(installed, capsys):
    installed.append(FakeEntryPoint("greet", greet))
    assert cli.main(["hub", "list"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'hub'" in err
    assert "installed: greet" in err
    assert cli.COMMANDS_GROUP in err and "huri-hub" in err


def test_a_broken_command_names_its_distribution(installed, capsys):
    broken = FakeEntryPoint("hub", ImportError("No module named 'tomli'"), "acme-hub")
    installed += [FakeEntryPoint("greet", greet), broken]
    assert cli.main(["hub", "list"]) == 1
    err = capsys.readouterr().err
    assert "'hub' (from acme-hub) cannot be imported" in err and "tomli" in err
    # It does not take the others down with it.
    assert cli.main(["greet"]) == 0
    assert cli.main(["--help"]) == 0
    assert "hub    (cannot be imported, from acme-hub)" in capsys.readouterr().out


def test_a_target_that_is_not_callable_is_rejected(installed, capsys):
    installed.append(FakeEntryPoint("odd", "not a function", "acme"))
    assert cli.main(["odd"]) == 1
    assert "not callable" in capsys.readouterr().err


def test_one_name_registered_twice_is_an_error_when_run(installed, capsys):
    installed += [
        FakeEntryPoint("hub", greet, dist="huri-hub"),
        FakeEntryPoint("hub", greet, dist="mallory-hub"),
    ]
    assert cli.main(["hub"]) == 1
    err = capsys.readouterr().err
    assert "registered twice" in err
    assert "huri-hub" in err and "mallory-hub" in err


def test_a_conflict_does_not_take_the_other_commands_down(installed, capsys):
    installed += [
        FakeEntryPoint("greet", greet),
        FakeEntryPoint("deploy", greet, dist="acme-deploy"),
        FakeEntryPoint("deploy", greet, dist="other-deploy"),
    ]
    assert cli.main(["greet", "ada"]) == 0
    assert capsys.readouterr().out == "hello ada\n"
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "deploy  (registered twice, run it for details)" in out
    assert "greet   Say hello to whoever is named." in out


def test_the_same_target_twice_is_not_a_conflict(installed):
    # One distribution seen twice on sys.path (e.g. an editable install).
    installed += [FakeEntryPoint("greet", greet), FakeEntryPoint("greet", greet)]
    assert cli.main(["greet"]) == 0


def test_core_registers_no_command():
    root = Path(__file__).resolve().parents[2]
    project = loads_toml((root / "pyproject.toml").read_text())["project"]
    assert cli.COMMANDS_GROUP not in project.get("entry-points", {})
    assert project["scripts"] == {"huri": "huri_core.cli:main"}


def test_the_huri_script_runs_as_a_module():
    res = subprocess.run(
        [sys.executable, "-m", "huri_core.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "usage: huri" in res.stdout
