"""`huri hub`: registry + GitHub topic, offline (every URL is faked).

The registry is written inline, in the schema of huri-modules' registry.toml:
huri-core is tested without huri-modules or any of its modules.
"""

import json

import pytest

from huri_core import hub
from huri_core.cli import main as huri

REGISTRY_TEXT = """
schema = 1

[[events]]
package = "huri-events"
namespace = "huri"
source = { path = "packages/huri-events" }

[[module]]
name = "stt"
package = "huri-module-stt"
summary = "Speech to text"
huri_core = ">=0.4.0.dev0,<0.5"
source = { path = "packages/huri-module-stt" }

[[module]]
name = "lights"
package = "huri-module-lights"
summary = "Mood lighting, a verified third-party module"
huri_core = ">=0.4.0.dev0,<0.5"
[module.source]  # pinned as a git submodule of huri-modules, at the same tag
git = "https://github.com/acme/huri-lights"
tag = "0.2.0"
submodule = "modules/lights"
subdirectory = "module"
"""


@pytest.fixture
def checkout(tmp_path):
    """A local huri-modules checkout: its registry, its checker, one package."""
    (tmp_path / "registry.toml").write_text(REGISTRY_TEXT)
    (tmp_path / "check_registry.py").write_text("")
    (tmp_path / "packages" / "huri-module-stt").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def registry(checkout):
    return str(checkout / "registry.toml")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No ambient HURI_* setting, and no checkout around the current directory."""
    for var in ("HURI_MODULES_REF", "HURI_REGISTRY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hub, "installed_core_version", lambda: "0.4.0")


SERVO_PYPROJECT = """
[project]
name = "huri-module-servo"
[project.entry-points."huri.modules"]
servo = "huri_servo.module:Servo"
"""
SERVO_MANIFEST = """
name = "servo"
summary = "Gesture to servo angle arrays"
input = { topic = "huri.motion", event = "huri_events.motion:Motion" }
output = { topic = "acme.servo_angles", event = "huri_events_servo:ServoAngles" }
requires = { actuation = true }
huri_core = ">=0.4.0.dev0,<0.5"
"""
OLD_PYPROJECT = SERVO_PYPROJECT.replace("servo", "oldie")
OLD_MANIFEST = SERVO_MANIFEST.replace("servo", "oldie").replace(
    ">=0.4.0.dev0,<0.5", ">=0.2,<0.3"
)


def raw(repo, path):
    return hub.RAW.format(repo=repo, ref="main", path=path)


SEARCH = {
    "items": [
        {
            "full_name": "acme/huri-module-servo",
            "description": "servos",
            "html_url": "https://github.com/acme/huri-module-servo",
            "default_branch": "main",
        },
        {
            "full_name": "bob/huri-module-oldie",
            "description": "old",
            "html_url": "https://github.com/bob/huri-module-oldie",
            "default_branch": "main",
        },
        {
            "full_name": "eve/not-a-module",
            "description": "no pyproject",
            "html_url": "https://github.com/eve/not-a-module",
            "default_branch": "main",
        },
    ]
}
WEB = {
    raw("acme/huri-module-servo", "pyproject.toml"): SERVO_PYPROJECT,
    raw("acme/huri-module-servo", "huri_servo/huri_module.toml"): SERVO_MANIFEST,
    raw("bob/huri-module-oldie", "pyproject.toml"): OLD_PYPROJECT,
    raw("bob/huri-module-oldie", "huri_oldie/huri_module.toml"): OLD_MANIFEST,
}


def fetch(url):
    if url.startswith(f"{hub.GITHUB_API}/search/repositories"):
        assert "topic%3Ahuri-module" in url
        return json.dumps(SEARCH)
    return WEB.get(url)


def run(argv, capsys):
    code = hub.main(argv, fetch=fetch)
    return code, capsys.readouterr().out


def test_search_lists_tagged_repositories(capsys):
    code, out = run(["search", "servo"], capsys)
    assert code == 0 and "acme/huri-module-servo" in out and "3 repositories" in out


def test_list_merges_verified_and_community_for_the_core_version(capsys, registry):
    code, out = run(["list", "--registry", registry, "--core", "0.4.0"], capsys)
    lines = {line.split()[0]: line for line in out.splitlines()[:-1]}
    assert "verified" in lines["stt"] and "verified" in lines["lights"]
    assert "community" in lines["servo"]
    assert "oldie" not in lines  # supports huri-core 0.2.x only
    assert out.splitlines()[-1] == f"{len(lines)} module(s) for huri-core 0.4.0"


def test_list_all_and_verified_filters(capsys, registry):
    _, out = run(["list", "--registry", registry, "--core", "0.4.0", "--all"], capsys)
    assert "oldie" in out
    _, out = run(["list", "--registry", registry, "--verified"], capsys)
    assert "servo" not in out and "stt" in out


def test_a_newer_core_hides_modules_built_for_this_one(capsys, registry):
    _, out = run(
        ["list", "--registry", registry, "--core", "0.5.0", "--no-crawl"], capsys
    )
    assert out.strip() == "0 module(s) for huri-core 0.5.0"


def test_install_a_curated_module_from_a_checkout(capsys, checkout):
    code, out = run(
        ["install", "stt", "--dry-run", "--registry", str(checkout)], capsys
    )
    assert code == 0
    assert "stt: verified" in out
    assert f"pip install {checkout / 'packages' / 'huri-module-stt'}" in out
    assert "not enabled" in out and "HURI_MODULES" in out


def test_the_checkout_around_the_current_directory_is_found(
    capsys, checkout, monkeypatch
):
    (checkout / "packages" / "huri-module-stt" / "src").mkdir()
    monkeypatch.chdir(checkout / "packages" / "huri-module-stt" / "src")
    code, out = run(["install", "stt", "--dry-run"], capsys)
    assert code == 0
    assert f"pip install {checkout / 'packages' / 'huri-module-stt'}" in out


def test_a_registry_without_its_checker_is_not_a_checkout(tmp_path):
    (tmp_path / "registry.toml").write_text(REGISTRY_TEXT)
    assert hub.find_local_registry(tmp_path) is None
    (tmp_path / "check_registry.py").write_text("")
    assert hub.find_local_registry(tmp_path / "a" / "b") == tmp_path / "registry.toml"
    assert hub.find_local_registry(tmp_path) == tmp_path / "registry.toml"


def test_huri_registry_points_at_another_registry(capsys, registry, monkeypatch):
    monkeypatch.setenv("HURI_REGISTRY", registry)
    _, out = run(["list", "--verified"], capsys)
    assert "stt" in out and "lights" in out


def test_a_verified_third_party_module_installs_at_its_reviewed_tag(capsys, registry):
    code, out = run(["install", "lights", "--dry-run", "--registry", registry], capsys)
    assert code == 0 and "lights: verified" in out
    target = "git+https://github.com/acme/huri-lights@0.2.0#subdirectory=module"
    assert f"pip install {target}" in out


def test_a_git_source_must_pin_a_tag(capsys, tmp_path):
    text = REGISTRY_TEXT.replace('tag = "0.2.0"\n', "")
    (tmp_path / "registry.toml").write_text(text)
    argv = ["list", "--registry", str(tmp_path / "registry.toml"), "--verified"]
    assert hub.main(argv, fetch=fetch) == 1
    assert "'lights': a git source must pin a `tag`" in (capsys.readouterr().err)


def test_a_module_for_another_core_is_not_installed(capsys, registry, monkeypatch):
    monkeypatch.setattr(hub, "installed_core_version", lambda: "0.5.0")
    argv = ["install", "stt", "--dry-run", "--registry", registry]
    assert hub.main(argv, fetch=fetch) == 1
    err = capsys.readouterr().err
    assert "supports huri-core >=0.4.0.dev0,<0.5, but 0.5.0" in err
    assert "HURI_MODULES_REF" in err


def test_a_community_module_is_only_installed_on_request(capsys, registry):
    code = hub.main(
        ["install", "servo", "--dry-run", "--registry", registry], fetch=fetch
    )
    err = capsys.readouterr().err
    assert code == 1 and "not reviewed" in err and "--community" in err

    argv = ["install", "servo", "--dry-run", "--community", "--registry", registry]
    code, out = run(argv, capsys)
    assert code == 0 and "community (not reviewed)" in out
    assert "pip install git+https://github.com/acme/huri-module-servo.git" in out


def test_install_by_url_and_unknown_name(capsys, registry):
    code, out = run(["install", "git+https://example.org/x.git", "--dry-run"], capsys)
    assert code == 0 and "pip install git+https://example.org/x.git" in out
    code = hub.main(
        ["install", "nope", "--dry-run", "--registry", registry], fetch=fetch
    )
    assert code == 1 and "no module named 'nope'" in capsys.readouterr().err


def test_remote_registry_installs_from_huri_modules_at_the_same_ref(monkeypatch):
    modules = {
        m.name: m
        for m in hub.load_registry(
            "https://example.org/registry.toml", fetch=lambda u: REGISTRY_TEXT
        )
    }
    repo = "git+https://github.com/Lucy-Robotics/huri-modules.git"
    assert modules["stt"].install == (
        f"{repo}@main#subdirectory=packages/huri-module-stt"
    )
    monkeypatch.setenv("HURI_MODULES_REF", "0.1.0")
    stt, _ = hub.load_registry("https://x/registry.toml", fetch=lambda u: REGISTRY_TEXT)
    assert stt.install == f"{repo}@0.1.0#subdirectory=packages/huri-module-stt"


def test_a_local_registry_outside_a_checkout_installs_from_github(tmp_path):
    (tmp_path / "registry.toml").write_text(REGISTRY_TEXT)
    stt, _ = hub.load_registry(str(tmp_path / "registry.toml"))
    assert stt.install == hub.curated_install("packages/huri-module-stt")


def test_registry_and_installs_use_one_huri_modules_ref(monkeypatch):
    raw = "https://raw.githubusercontent.com/Lucy-Robotics/huri-modules"
    assert hub.modules_ref() == "main"
    assert hub.default_registry() == f"{raw}/main/registry.toml"
    monkeypatch.setenv("HURI_MODULES_REF", "0.1.0")
    assert hub.default_registry() == f"{raw}/0.1.0/registry.toml"
    assert hub.curated_install("packages/huri-events").startswith(
        "git+https://github.com/Lucy-Robotics/huri-modules.git@0.1.0#"
    )


def test_the_default_registry_is_read_from_github(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # not in a checkout
    seen = []

    def remote(url):
        seen.append(url)
        return REGISTRY_TEXT if url == hub.default_registry() else fetch(url)

    assert hub.main(["list", "--verified"], fetch=remote) == 0
    assert seen[0] == hub.default_registry()
    assert "stt" in capsys.readouterr().out


def test_huri_command_dispatches_to_hub(capsys):
    assert huri(["--help"]) == 0
    assert huri(["nope"]) == 2


def test_a_missing_registry_is_a_readable_error(capsys, tmp_path):
    argv = ["list", "--registry", "https://example.org/none.toml"]
    assert hub.main(argv, fetch=lambda url: None) == 1
    assert "no registry at" in capsys.readouterr().err
    argv = ["list", "--registry", str(tmp_path / "none.toml")]
    assert hub.main(argv, fetch=fetch) == 1
    assert "no registry at" in capsys.readouterr().err


def test_a_malformed_registry_is_a_readable_error(capsys, tmp_path):
    (tmp_path / "registry.toml").write_text("[[module]\n")
    argv = ["list", "--registry", str(tmp_path / "registry.toml")]
    assert hub.main(argv, fetch=fetch) == 1
    assert "not valid TOML" in capsys.readouterr().err


def test_a_name_claimed_by_several_repositories_is_refused(capsys, registry):
    twin = dict(SEARCH["items"][0], full_name="mallory/huri-module-servo")
    twin["html_url"] = "https://github.com/mallory/huri-module-servo"
    web = dict(WEB)
    web[raw("mallory/huri-module-servo", "pyproject.toml")] = SERVO_PYPROJECT
    web[raw("mallory/huri-module-servo", "huri_servo/huri_module.toml")] = (
        SERVO_MANIFEST
    )

    def two_claimants(url):
        if url.startswith(f"{hub.GITHUB_API}/search/repositories"):
            return json.dumps({"items": SEARCH["items"] + [twin]})
        return web.get(url)

    argv = ["install", "servo", "--dry-run", "--community", "--registry", registry]
    assert hub.main(argv, fetch=two_claimants) == 1
    err = capsys.readouterr().err
    assert "2 community repositories provide 'servo'" in err
    assert "acme/huri-module-servo" in err and "mallory/huri-module-servo" in err


def test_an_invalid_core_version_is_a_readable_error(capsys, registry):
    assert (
        hub.main(["list", "--core", "zero.four", "--registry", registry], fetch=fetch)
        == 1
    )
    assert "invalid huri-core version 'zero.four'" in capsys.readouterr().err


def test_missing_client_extra_is_explained(capsys, monkeypatch):
    def needs_packaging(*_):
        raise ModuleNotFoundError("No module named 'packaging'")

    monkeypatch.setattr(hub, "cmd_list", needs_packaging)
    assert hub.main(["list"], fetch=fetch) == 1
    assert 'pip install "huri-core[client]"' in capsys.readouterr().err
