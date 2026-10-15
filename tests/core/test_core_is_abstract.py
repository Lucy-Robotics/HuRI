"""huri-core defines and imports no concrete event (design §2), and as a
whole (code and tests) depends on no module, event or command package: those
live in huri-modules, which depends on huri-core, never the other way round."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
PACKAGE = ROOT / "huri_core"
BASES = {"EventData", "JsonEvent", "BytesEvent"}


def framework_files():
    """All of huri_core: the server, the client SDK, the `huri` command..."""
    yield from PACKAGE.rglob("*.py")


def test_framework_defines_only_the_base_event_classes():
    for path in framework_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(b).split("[")[0].split(".")[-1] for b in node.bases}
            if bases & BASES:
                assert path.name == "events.py" and node.name in BASES, (
                    f"{path.relative_to(ROOT)} defines event {node.name}: concrete "
                    "events belong in an event package (huri-events)"
                )


def imported_packages(path: Path):
    """Top-level packages a file imports (absolute imports only)."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield (node.module or "").split(".")[0]


def test_framework_never_imports_curated_events():
    for path in framework_files():
        names = set(imported_packages(path))
        assert "huri_events" not in names, f"{path.relative_to(ROOT)} imports {names}"


def test_core_and_its_tests_import_no_module_or_event_package():
    """Not huri_events, huri_stt, huri_hub...: core is tested with none
    installed."""
    files = [*framework_files(), *TESTS.rglob("*.py")]
    for path in files:
        foreign = {
            name
            for name in imported_packages(path)
            if name.startswith("huri_") and name != "huri_core"
        }
        assert not foreign, f"{path.relative_to(ROOT)} imports {sorted(foreign)}"
