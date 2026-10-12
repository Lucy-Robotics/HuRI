#!/usr/bin/env python3
"""Check the links of the landing pages (run by CI).

    python landing/check_links.py [--external] [FILE_OR_DIR ...]

Relative links must point to an existing file or directory, and a `#anchor`
into a Markdown file to one of its headings (GitHub's slugs). External links
are only fetched with --external: the landing page links to repositories that
do not all exist yet, and CI should not depend on the network.
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterable, List

HERE = Path(__file__).resolve().parent
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"(?ms)^```.*?^```")


def slug(heading: str) -> str:
    # GitHub: lowercase, drop punctuation but keep "-" and "_", spaces -> "-".
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set:
    text = _FENCE.sub("", path.read_text())
    return {slug(m.group(1)) for m in re.finditer(r"(?m)^#+\s+(.*)$", text)}


def links(path: Path) -> Iterable[str]:
    return _LINK.findall(_FENCE.sub("", path.read_text()))


def check_file(path: Path, external: bool) -> List[str]:
    problems = []
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


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path, default=[HERE])
    parser.add_argument("--external", action="store_true")
    args = parser.parse_args(argv)

    files: List[Path] = []
    for path in args.paths:
        files += sorted(path.rglob("*.md")) if path.is_dir() else [path]
    problems = [p for f in files for p in check_file(f, args.external)]
    for problem in problems:
        print(f"- {problem}")
    print(
        f"{len(files)} file(s), "
        + ("links OK" if not problems else f"{len(problems)} broken")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
