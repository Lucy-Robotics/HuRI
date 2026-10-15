"""huri-modules/registry.toml and its checker."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "huri-modules" / "registry.toml"
spec = importlib.util.spec_from_file_location(
    "check_registry", ROOT / "huri-modules" / "check_registry.py"
)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def check(tmp_path):
    def run(text):
        path = tmp_path / "registry.toml"
        path.write_text(text)
        return checker.check(path, ROOT)

    return run


def without_entry(text, name):
    blocks = text.split("\n[[module]]\n")
    kept = [b for b in blocks if not b.startswith(f'name = "{name}"')]
    return "\n[[module]]\n".join(kept)


def test_the_registry_is_valid():
    assert checker.check(REGISTRY, ROOT) == []


def test_a_curated_module_missing_from_the_registry_is_reported(check):
    problems = check(without_entry(REGISTRY.read_text(), "tag"))
    assert problems == ["curated module 'tag' (packages/) is not in the registry"]


@pytest.mark.parametrize(
    "old,new,fragment",
    [
        (
            'package = "huri-module-tag"',
            'package = "huri-module-tagger"',
            "is huri-module-tag, not",
        ),
        (
            'source = { path = "packages/huri-module-tag" }',
            'source = { path = "packages/nope" }',
            "no package at source",
        ),
        (
            "Stitches overlapping transcript passes into one sentence",
            "Something else",
            "summary",
        ),
    ],
)
def test_an_entry_that_disagrees_with_its_package(check, old, new, fragment):
    text = REGISTRY.read_text()
    assert old in text
    problems = check(text.replace(old, new, 1))
    assert any(fragment in p for p in problems), problems


def test_verified_must_be_a_date(check):
    text = re.sub(
        r"verified = 2026-10-12",
        'verified = "yesterday"',
        REGISTRY.read_text(),
        count=2,
    )
    assert any("'verified' must be a date" in p for p in check(text))


def test_an_unknown_module_name(check):
    text = REGISTRY.read_text().replace('name = "tag"', 'name = "tagx"', 1)
    problems = check(text)
    assert any("module 'tagx'" in p and "provides ['tag']" in p for p in problems)


def test_events_namespace_must_belong_to_the_listed_package(check):
    text = REGISTRY.read_text().replace('namespace = "huri"', 'namespace = "acme"', 1)
    assert any("namespace 'acme'" in p for p in check(text))
