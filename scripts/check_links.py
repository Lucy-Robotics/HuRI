#!/usr/bin/env python3
"""Check the links of huri-core's pages (run by CI).

    python scripts/check_links.py [--external] [--siblings DIR] [FILE_OR_DIR ...]

By default: README.md, CONTRIBUTING.md, RELEASING.md and docs/. Relative links
must point to an existing file or directory, and a `#anchor` into a Markdown
file to one of its headings (GitHub's slugs). A URL into the Lucy-Robotics
organization, anywhere in a page (code blocks included, so the install strings
are checked too), must name one of the HuRI repositories or the use cases built
on it. Two names are errors: `Lucy-Robotics/HuRI`, the old name of huri-core
(GitHub redirects it, but a page must not rely on that), and `HuRI-landing`,
archived once its pages moved here.

With --siblings DIR (or HURI_SPLIT_DIR), a deep link into a known repository
(`<repo>/tree|blob/<ref>/<path>`, `<repo>#anchor`, a pip string's
`#subdirectory=<path>`) is also resolved against the checkout DIR/<repo>: the
path must exist there, and an anchor must name one of its headings. The ref in
the URL is not checked out: DIR holds whatever ref the caller checked out.

External links are only fetched with --external: CI should not depend on the
network.
"""

import argparse
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["README.md", "CONTRIBUTING.md", "RELEASING.md", "docs"]
ORG = "Lucy-Robotics"
CORE = f"https://github.com/{ORG}/huri-core"
# Spelled as on GitHub (which compares names case-insensitively).
REPOS = {
    "huri-core",
    "huri-modules",
    "huri-plugin-template",
    # Use cases: clients built on HuRI, each in its own repository.
    "HuRI-Cli",
    "HuRI-inmoov",
    "HuRI_ATP_testsite",
    "HuRI_website_demo",
}
# Names a page must not use, lowercased, and what to write instead.
RETIRED = {
    # Renamed at the split; the name is never reused.
    "huri": f"{ORG}/HuRI is the old name, now huri-core; use the huri-core URL "
    f"({CORE})",
    # Archived: its pages moved into huri-core.
    "huri-landing": f"{ORG}/HuRI-landing is archived and its pages moved to "
    f"huri-core: the ecosystem is {CORE}#huri-ecosystem, the architecture "
    f"{CORE}/blob/main/docs/ARCHITECTURE.md, contributing "
    f"{CORE}/blob/main/CONTRIBUTING.md",
}
_CANONICAL = {name.lower(): name for name in REPOS}

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_ORG_URL = re.compile(
    rf"(git\+)?https?://github\.com/(?i:{ORG})/"
    # The repository, without its ".git" or the sentence's punctuation.
    r"([A-Za-z0-9_-]+(?:\.(?!git\b)[A-Za-z0-9_-]+)*)(?:\.git)?"
    r"(/[^\s#)\]\"'`<>]*)?"  # /tree/<ref>/<path>, /issues/...
    r"(?:@(<[^>\s]*>|[^\s#)\]\"'`<>]+))?"  # a pip string's @<ref>
    r"(#[^\s)\]\"'`<>]*)?"  # #anchor, #subdirectory=<path>
)
_TRAILING = ".,;:"


def slug(heading: str) -> str:
    # GitHub: lowercase, drop punctuation but keep "-" and "_", spaces -> "-".
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> Set[str]:
    """GitHub's heading anchors; a repeated heading gets `-1`, `-2`, ..."""
    text = _FENCE.sub("", path.read_text())
    seen: Dict[str, int] = {}
    result = set()
    for m in re.finditer(r"(?m)^#+\s+(.*)$", text):
        base = slug(m.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def links(path: Path) -> Iterable[str]:
    return _LINK.findall(_INLINE_CODE.sub("", _FENCE.sub("", path.read_text())))


def check_sibling(
    where: str, siblings: Path, repo: str, url_path: str, fragment: str
) -> List[str]:
    """A deep link into `repo`, resolved against the checkout siblings/repo."""
    if url_path:
        parts = url_path.strip("/").split("/")
        if len(parts) < 2 or parts[0] not in ("tree", "blob"):
            return []  # issues, pulls, releases: not files of the repository
        rel, anchor = "/".join(parts[2:]), fragment
    elif fragment.startswith("subdirectory="):
        rel, anchor = fragment[len("subdirectory=") :], ""
    elif fragment and fragment != "readme":  # #readme: GitHub's own anchor
        rel, anchor = "", fragment
    else:
        return []
    root = siblings / repo
    if not root.is_dir():
        return [f"{where} -> {root} is not checked out"]
    target = root / rel if rel else root
    if not target.exists():
        return [f"{where} -> {repo} has no {rel} (in {root})"]
    if target.is_dir():
        target = target / "README.md"
    if anchor and target.suffix == ".md":
        if not target.exists() or anchor not in anchors(target):
            return [f"{where} -> no heading #{anchor} in {target}"]
    return []


def check_org_urls(path: Path, siblings: Optional[Path] = None) -> List[str]:
    """Every Lucy-Robotics URL in the page names a known repository."""
    problems = []
    for match in _ORG_URL.finditer(path.read_text()):
        name = match.group(2)
        url_path = (match.group(3) or "").rstrip(_TRAILING)
        fragment = (match.group(5) or "").lstrip("#").rstrip(_TRAILING)
        where = f"{path}: {match.group(0).rstrip(_TRAILING)}"
        repo = _CANONICAL.get(name.lower())
        if name.lower() in RETIRED:
            problems.append(f"{where} -> {RETIRED[name.lower()]}")
        elif repo is None:
            problems.append(
                f"{where} -> unknown repository {ORG}/{name} "
                f"(known: {', '.join(sorted(REPOS))})"
            )
        elif siblings is not None:
            problems += check_sibling(where, siblings, repo, url_path, fragment)
    return problems


def check_file(
    path: Path, external: bool, siblings: Optional[Path] = None
) -> List[str]:
    problems = check_org_urls(path, siblings)
    for target in links(path):
        if target.startswith(("http://", "https://")):
            if external:
                try:
                    request = urllib.request.Request(target, method="HEAD")
                    urllib.request.urlopen(request, timeout=10)
                except Exception as e:  # noqa: BLE001 - report any failure
                    problems.append(f"{path}: {target} -> {e}")
            continue
        if target.startswith("mailto:"):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            problems.append(f"{path}: {target} -> {resolved} does not exist")
        elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            problems.append(f"{path}: {target} -> no heading #{anchor}")
    return problems


def markdown_files(path: Path) -> List[Path]:
    if not path.is_dir():
        return [path]
    # Skip hidden directories (.git, .venv) and anything vendored in them.
    return sorted(
        p
        for p in path.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(path).parts)
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "paths", nargs="*", type=Path, default=[ROOT / page for page in PAGES]
    )
    parser.add_argument("--external", action="store_true")
    parser.add_argument(
        "--siblings",
        type=Path,
        default=os.environ.get("HURI_SPLIT_DIR") or None,
        metavar="DIR",
        help="checkouts of the HuRI repositories, as DIR/<repo>; deep links "
        "into them are checked (default: $HURI_SPLIT_DIR)",
    )
    args = parser.parse_args(argv)

    files = [f for path in args.paths for f in markdown_files(path)]
    problems = [p for f in files for p in check_file(f, args.external, args.siblings)]
    for problem in problems:
        print(f"- {problem}")
    print(
        f"{len(files)} file(s), "
        + ("links OK" if not problems else f"{len(problems)} broken")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
