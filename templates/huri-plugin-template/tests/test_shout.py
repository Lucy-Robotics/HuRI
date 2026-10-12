"""Test the module alone: no server, no Ray (huri_core.testing)."""

from huri_events.speech import Transcript
from huri_events_example import Shout
from huri_example.module import Shouter

from huri_core.core.compat import check_manifests, check_modules
from huri_core.core.plugins import find_manifest, load_events, load_modules
from huri_core.testing import ModuleHarness


def test_shouts_finished_transcripts_only():
    out = ModuleHarness(Shouter, exclamations=2).run(
        Transcript("hello", False), Transcript("hello robot", True)
    )
    assert out == [Shout("HELLO ROBOT!!")]


def test_installed_module_passes_the_server_checks():
    """What the server verifies at startup: entry point, events, manifest."""
    modules = load_modules(["shout"])
    events = load_events()
    check_modules(modules, events)
    manifest = find_manifest("shout")
    assert manifest is not None
    check_manifests(modules, {"shout": manifest}, events)
