"""`huri hub`: registry + GitHub topic, offline (every URL is faked)."""

import json
from pathlib import Path

from huri_core import hub
from huri_core.cli import main as huri

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = str(ROOT / "huri-modules" / "registry.toml")

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


def test_list_merges_verified_and_community_for_the_core_version(capsys):
    code, out = run(["list", "--registry", REGISTRY, "--core", "0.4.0"], capsys)
    lines = {line.split()[0]: line for line in out.splitlines()[:-1]}
    assert "verified" in lines["stt"] and "community" in lines["servo"]
    assert "oldie" not in lines  # supports huri-core 0.2.x only
    assert out.splitlines()[-1] == f"{len(lines)} module(s) for huri-core 0.4.0"


def test_list_all_and_verified_filters(capsys):
    _, out = run(["list", "--registry", REGISTRY, "--core", "0.4.0", "--all"], capsys)
    assert "oldie" in out
    _, out = run(["list", "--registry", REGISTRY, "--verified"], capsys)
    assert "servo" not in out and "stt" in out


def test_a_newer_core_hides_modules_built_for_this_one(capsys):
    _, out = run(
        ["list", "--registry", REGISTRY, "--core", "0.5.0", "--no-crawl"], capsys
    )
    assert out.strip() == "0 module(s) for huri-core 0.5.0"


def test_install_a_verified_module_from_a_checkout(capsys):
    code, out = run(["install", "stt", "--dry-run", "--registry", REGISTRY], capsys)
    assert code == 0
    assert "stt: verified" in out
    assert f"pip install {ROOT / 'packages' / 'huri-module-stt'}" in out
    assert "not enabled" in out and "HURI_MODULES" in out


def test_install_a_community_module_says_it_is_not_reviewed(capsys):
    code, out = run(["install", "servo", "--dry-run", "--registry", REGISTRY], capsys)
    assert code == 0 and "community (not reviewed)" in out
    assert "pip install git+https://github.com/acme/huri-module-servo.git" in out


def test_install_by_url_and_unknown_name(capsys):
    code, out = run(["install", "git+https://example.org/x.git", "--dry-run"], capsys)
    assert code == 0 and "pip install git+https://example.org/x.git" in out
    code = hub.main(
        ["install", "nope", "--dry-run", "--registry", REGISTRY], fetch=fetch
    )
    assert code == 1 and "no module named 'nope'" in capsys.readouterr().err


def test_remote_registry_installs_from_the_huri_repository():
    text = (ROOT / "huri-modules" / "registry.toml").read_text()
    modules = hub.load_registry(
        "https://example.org/registry.toml", fetch=lambda u: text
    )
    stt = next(m for m in modules if m.name == "stt")
    repo = "git+https://github.com/Lucy-Robotics/HuRI.git"
    assert stt.install == f"{repo}#subdirectory=packages/huri-module-stt"


def test_huri_command_dispatches_to_hub(capsys):
    assert huri(["--help"]) == 0
    assert huri(["nope"]) == 2


def test_a_missing_registry_is_a_readable_error(capsys):
    argv = ["list", "--registry", "https://example.org/none.toml"]
    assert hub.main(argv, fetch=lambda url: None) == 1
    assert "no registry at" in capsys.readouterr().err
