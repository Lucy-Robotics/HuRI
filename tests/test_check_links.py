"""huri-core's pages' links resolve, and scripts/check_links.py catches broken ones.

No URL is fetched: the checker runs offline, as in CI.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_links", ROOT / "scripts" / "check_links.py"
)
assert spec and spec.loader
check_links = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_links)
GH = "https://github.com/Lucy-Robotics"


def test_pages_have_no_broken_links(monkeypatch):
    # Offline only: a developer's $HURI_SPLIT_DIR must not add deep-link checks.
    monkeypatch.delenv("HURI_SPLIT_DIR", raising=False)
    names = ["README.md", "CONTRIBUTING.md", "RELEASING.md", "docs"]
    assert check_links.PAGES == names
    files = [f for name in names for f in check_links.markdown_files(ROOT / name)]
    assert ROOT / "docs" / "ARCHITECTURE.md" in files
    assert check_links.main([str(ROOT / name) for name in names]) == 0
    # The default is the same set of pages.
    assert check_links.main([]) == 0


def test_the_readme_has_the_ecosystem_section():
    # Other repositories link to huri-core#huri-ecosystem: keep the anchor.
    assert "huri-ecosystem" in check_links.anchors(ROOT / "README.md")


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


def test_known_repositories_pass(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "[core](https://github.com/Lucy-Robotics/huri-core)\n"
        "[stt](https://github.com/Lucy-Robotics/huri-modules/tree/main/packages/x)\n"
        "[tpl](https://github.com/Lucy-Robotics/huri-plugin-template#readme)\n"
        "[robot](https://github.com/Lucy-Robotics/HuRI-InMoov)\n"
        "[cli](https://github.com/Lucy-Robotics/HuRI-CLI/issues)\n"
        "[demo](https://github.com/Lucy-Robotics/HuRI_website_demo)\n"
        "[atp](https://github.com/lucy-robotics/huri_atp_testsite)\n"
        "```sh\n"
        "pip install git+https://github.com/Lucy-Robotics/huri-core.git@dev\n"
        "pip install git+https://github.com/Lucy-Robotics/HuRI-CLI.git@0.1.0\n"
        "```\n"
    )
    assert check_links.check_file(page, external=False) == []


def test_unknown_repository_is_reported(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "[old](https://github.com/Lucy-Robotics/huri-events)\n"
        "[sic](https://github.com/Lucy-Robotics/HuRI-website-demo)\n"
        "```sh\n"
        "pip install git+https://github.com/Lucy-Robotics/huri-server.git@0.4.0\n"
        "```\n"
    )
    problems = check_links.check_file(page, external=False)
    assert len(problems) == 3
    assert "unknown repository Lucy-Robotics/huri-events" in problems[0]
    assert "unknown repository Lucy-Robotics/HuRI-website-demo" in problems[1]
    assert "unknown repository Lucy-Robotics/huri-server (" in problems[2]


def test_the_archived_landing_repository_is_reported(tmp_path):
    # HuRI-landing's pages moved here: a link to it must name the new place.
    page = tmp_path / "page.md"
    page.write_text(
        "[home](https://github.com/Lucy-Robotics/HuRI-landing)\n"
        f"see {GH}/huri-landing/blob/main/ARCHITECTURE.md\n"
        "```sh\n"
        f'pip install "git+{GH}/HuRI-landing.git@0.3.0"\n'
        "```\n"
    )
    problems = check_links.check_file(page, external=False)
    assert len(problems) == 3
    for problem in problems:
        assert "HuRI-landing is archived and its pages moved to huri-core" in problem
        assert f"{GH}/huri-core#huri-ecosystem" in problem
        assert "docs/ARCHITECTURE.md" in problem


def test_the_old_name_is_reported(tmp_path):
    # Lucy-Robotics/HuRI was renamed to huri-core: GitHub redirects it, but
    # neither a link nor an install string should rely on the redirect.
    page = tmp_path / "page.md"
    page.write_text(
        "[home](https://github.com/Lucy-Robotics/HuRI)\n"
        "see https://github.com/lucy-robotics/huri/blob/main/README.md\n"
        "```sh\n"
        f'pip install "huri-core @ git+{GH}/HuRI.git@0.3.0"\n'
        "```\n"
    )
    problems = check_links.check_file(page, external=False)
    assert len(problems) == 3
    for problem in problems:
        assert "old name, now huri-core; use the huri-core URL" in problem


def test_repository_names_in_running_text(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "<https://github.com/Lucy-Robotics/huri-evnts>\n"
        "https://github.com/Lucy-Robotics/huri-evnts, here\n"
        "https://github.com/lucy-robotics/huri-evnts\n"
        "Repo https://github.com/Lucy-Robotics/huri-core.\n"
        "Lowercase org https://github.com/lucy-robotics/HURI-MODULES; fine\n"
        "<https://github.com/Lucy-Robotics/huri-core>, and\n"
        'pip install "git+https://github.com/Lucy-Robotics/huri-core.git@dev"\n'
    )
    problems = check_links.check_file(page, external=False)
    assert len(problems) == 3
    for problem in problems:
        assert "unknown repository Lucy-Robotics/huri-evnts " in problem


def test_hidden_directories_are_skipped(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "vendored.md").write_text("[gone](missing.md)\n")
    (tmp_path / "page.md").write_text("# Page\n")
    assert check_links.markdown_files(tmp_path) == [tmp_path / "page.md"]


def test_github_heading_slugs():
    assert check_links.slug("Client SDK") == "client-sdk"
    assert (
        check_links.slug("Module manifest (`huri_module.toml`)")
        == "module-manifest-huri_moduletoml"
    )
    assert (
        check_links.slug("Versions, and how a core change propagates")
        == "versions-and-how-a-core-change-propagates"
    )


def test_duplicate_headings_get_numbered_anchors(tmp_path):
    (tmp_path / "other.md").write_text("# Dup\n\n## Dup\n")
    page = tmp_path / "page.md"
    page.write_text("[a](other.md#dup) [b](other.md#dup-1) [c](other.md#dup-2)\n")
    (problem,) = check_links.check_file(page, external=False)
    assert "#dup-2" in problem


def test_tilde_fences_and_inline_code_are_skipped(tmp_path):
    (tmp_path / "other.md").write_text("# Real\n\n~~~\n# Not a heading\n~~~\n")
    page = tmp_path / "page.md"
    page.write_text(
        "~~~md\n[in code](nofile.md)\n~~~\n"
        "`[inline](nofile.md)` [ok](other.md#real) [bad](other.md#not-a-heading)\n"
    )
    (problem,) = check_links.check_file(page, external=False)
    assert "#not-a-heading" in problem


def _siblings(root):
    modules = root / "huri-modules"
    (modules / "packages" / "huri-module-stt").mkdir(parents=True)
    (modules / "packages" / "huri-events").mkdir(parents=True)
    (modules / "registry.toml").write_text("")
    (modules / "README.md").write_text("# huri-modules\n## Install\n")
    (root / "huri-core").mkdir()
    (root / "huri-core" / "README.md").write_text("# huri-core\n## Client SDK\n")
    (root / "HuRI-CLI" / "huri-meta").mkdir(parents=True)
    return root


def test_deep_links_resolve_against_sibling_checkouts(tmp_path):
    siblings = _siblings(tmp_path / "split")
    page = tmp_path / "page.md"
    page.write_text(
        f"[ok]({GH}/huri-modules/tree/main/packages/huri-module-stt)\n"
        f"[ok]({GH}/huri-modules/blob/main/registry.toml)\n"
        f"[ok]({GH}/huri-core#client-sdk)\n"
        f"[ok]({GH}/huri-modules/blob/main/README.md#install)\n"
        f"[ok]({GH}/huri-core/issues/93)\n"
        f"[ok]({GH}/huri-core#readme)\n"
        f"[ok]({GH}/HuRI-CLI/tree/main/huri-meta)\n"
        f"[gone]({GH}/HuRI-CLI/tree/main/examples/cli)\n"
        f"[typo]({GH}/huri-modules/tree/main/packages/huri-module-sst)\n"
        f"[anchor]({GH}/huri-core#client-sdj)\n"
        "```sh\n"
        f'pip install "huri-events @ git+{GH}/huri-modules.git@<ref>'
        '#subdirectory=packages/huri-events"\n'
        f'pip install "git+{GH}/huri-modules.git@0.1.0'
        '#subdirectory=packages/huri-evnts"\n'
        "```\n"
        f"[unchecked]({GH}/huri-plugin-template/blob/main/README.md)\n"
    )
    # Without the sibling checkouts only repository names are checked.
    assert check_links.check_file(page, external=False) == []
    problems = check_links.check_file(page, external=False, siblings=siblings)
    assert len(problems) == 5
    assert "has no examples/cli" in problems[0]
    assert "has no packages/huri-module-sst" in problems[1]
    assert "no heading #client-sdj" in problems[2]
    assert "has no packages/huri-evnts" in problems[3]
    assert "huri-plugin-template is not checked out" in problems[4]
    assert check_links.main(["--siblings", str(siblings), str(page)]) == 1
