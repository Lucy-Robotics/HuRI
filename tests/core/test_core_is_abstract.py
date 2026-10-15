"""huri-core's framework defines and imports no concrete event (design §2)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK = [ROOT / "huri_core" / "core", ROOT / "huri_core" / "testing.py"]
BASES = {"EventData", "JsonEvent", "BytesEvent"}


def framework_files():
    for path in FRAMEWORK:
        yield from (path.rglob("*.py") if path.is_dir() else [path])


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


def test_framework_never_imports_curated_events():
    for path in framework_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(
                n.split(".")[0] == "huri_events" for n in names
            ), f"{path.relative_to(ROOT)} imports {names}"
