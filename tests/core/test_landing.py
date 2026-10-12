"""The landing pages' links resolve, and the checker catches broken ones."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "check_links", ROOT / "landing" / "check_links.py"
)
assert spec and spec.loader
check_links = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_links)


def test_landing_and_readmes_have_no_broken_links():
    targets = [
        "landing",
        "README.md",
        "huri-modules",
        "templates",
        "examples",
        "packages",
    ]
    assert check_links.main([str(ROOT / t) for t in targets]) == 0


def test_broken_paths_and_anchors_are_reported(tmp_path):
    (tmp_path / "other.md").write_text("# Real Heading\n")
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md) [ok](other.md#real-heading) [web](https://example.invalid/x)\n"
        "[gone](missing.md) [bad anchor](other.md#nope)\n"
        "```\n[in code](ignored.md)\n```\n"
    )
    problems = check_links.check_file(page, external=False)
    assert len(problems) == 2
    assert "missing.md" in problems[0] and "#nope" in problems[1]


def test_github_heading_slugs():
    assert check_links.slug("Client SDK") == "client-sdk"
    assert (
        check_links.slug("Module manifest (`huri_module.toml`)")
        == "module-manifest-huri_moduletoml"
    )
