"""The docs link to things that exist (offline: no URL is fetched).

Relative links must point to an existing file, and a `#anchor` to a heading of
the target Markdown file (GitHub's slugs). A URL into the Lucy-Robotics
organization must name one of the HuRI repositories: the old `HuRI` name now
only redirects here, so docs name huri-core (RELEASING.md documents the
redirect, and may spell the old name).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["README.md", "RELEASING.md"]
REPOS = {
    "huri-core",
    "huri-modules",
    "huri-plugin-template",
    "huri-cli",
    "HuRI-landing",
}
OLD_NAME_DOCUMENTED_IN = {"RELEASING.md"}

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"(?ms)^```.*?^```")
_ORG_URL = re.compile(
    r"https?://(?:github\.com|raw\.githubusercontent\.com)/Lucy-Robotics/"
    r"([A-Za-z0-9_.-]+?)(?:\.git)?(?=[/@#)\s\"'`]|$)"
)


def slug(heading: str) -> str:
    # GitHub: lowercase, drop punctuation but keep "-" and "_", spaces -> "-".
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set:
    text = _FENCE.sub("", path.read_text())
    return {slug(m.group(1)) for m in re.finditer(r"(?m)^#+\s+(.*)$", text)}


def test_slugs_follow_github():
    assert slug("Versioning and compatibility") == "versioning-and-compatibility"
    assert slug("Finding modules: `huri hub`") == "finding-modules-huri-hub"


@pytest.mark.parametrize("page", PAGES)
def test_relative_links_and_anchors_exist(page):
    path = ROOT / page
    broken = []
    for target in _LINK.findall(_FENCE.sub("", path.read_text())):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            broken.append(f"{target}: {resolved} does not exist")
        elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            broken.append(f"{target}: no heading #{anchor}")
    assert not broken, broken


@pytest.mark.parametrize("page", PAGES)
def test_organization_urls_name_a_huri_repository(page):
    known = REPOS | ({"HuRI"} if page in OLD_NAME_DOCUMENTED_IN else set())
    repos = set(_ORG_URL.findall((ROOT / page).read_text()))
    assert repos and repos <= known, repos - known
