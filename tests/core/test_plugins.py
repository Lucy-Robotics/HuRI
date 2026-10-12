"""Entry-point discovery of modules and events (huri_core.core.plugins)."""

import os
from types import SimpleNamespace

import pytest

from huri_core.core import plugins
from huri_core.core.dataclasses.config import ModuleConfig
from huri_core.core.events import JsonEvent
from huri_core.core.module import Module
from huri_core.modules.factory import ModuleFactory


class ToyModule(Module):
    input_type = "toy.in"
    output_type = "toy.out"

    def __init__(self, greeting: str = "hi"):
        self.greeting = greeting


class ToyEvent(JsonEvent):
    pass


class OtherToyEvent(JsonEvent):
    pass


class FakeEntryPoint:
    def __init__(self, name, target, dist="pkg-a", value=None):
        self.name = name
        self.value = value or f"{dist}:{name}"
        self.dist = SimpleNamespace(name=dist)
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


@pytest.fixture
def installed(monkeypatch):
    """Replace the installed entry points with a fake set."""

    registry = {plugins.MODULES_GROUP: [], plugins.EVENTS_GROUP: []}
    monkeypatch.setattr(plugins, "entry_points", lambda group: registry[group])
    return registry


def test_module_is_found_through_its_entry_point(installed):
    installed[plugins.MODULES_GROUP].append(FakeEntryPoint("toy", ToyModule))
    assert plugins.load_modules(["toy"]) == {"toy": ToyModule}
    assert plugins.installed_modules() == ["toy"]


def test_only_selected_modules_are_imported(installed):
    broken = FakeEntryPoint("heavy", ImportError("No module named 'torch'"))
    installed[plugins.MODULES_GROUP] += [FakeEntryPoint("toy", ToyModule), broken]
    assert plugins.load_modules(["toy"]) == {"toy": ToyModule}


def test_broken_module_names_its_distribution(installed):
    installed[plugins.MODULES_GROUP].append(
        FakeEntryPoint("heavy", ImportError("No module named 'torch'"), dist="acme")
    )
    with pytest.raises(plugins.PluginError, match="acme.*torch"):
        plugins.load_modules(["heavy"])


def test_unknown_module_is_rejected(installed):
    installed[plugins.MODULES_GROUP].append(FakeEntryPoint("toy", ToyModule))
    with pytest.raises(ValueError, match="nope"):
        plugins.load_modules(["toy", "nope"])


def test_no_installed_module_explains_how_to_install(installed):
    with pytest.raises(ValueError, match="pip install"):
        plugins.load_modules(["stt"])


def test_non_module_entry_point_is_rejected(installed):
    installed[plugins.MODULES_GROUP].append(FakeEntryPoint("toy", object))
    with pytest.raises(plugins.PluginError, match="Module subclass"):
        plugins.load_modules(["toy"])


def test_same_module_name_from_two_distributions_is_an_error(installed):
    installed[plugins.MODULES_GROUP] += [
        FakeEntryPoint("toy", ToyModule, dist="pkg-a"),
        FakeEntryPoint("toy", ToyModule, dist="pkg-b"),
    ]
    with pytest.raises(plugins.PluginError, match="pkg-a.*pkg-b"):
        plugins.load_modules(["toy"])


def test_events_are_found_by_topic(installed):
    installed[plugins.EVENTS_GROUP].append(FakeEntryPoint("toy.in", ToyEvent))
    assert plugins.load_events() == {"toy.in": ToyEvent}


def test_one_topic_with_two_classes_names_both_distributions(installed):
    installed[plugins.EVENTS_GROUP] += [
        FakeEntryPoint("toy.in", ToyEvent, dist="pkg-a"),
        FakeEntryPoint("toy.in", OtherToyEvent, dist="pkg-b"),
    ]
    with pytest.raises(plugins.PluginError, match="pkg-a.*pkg-b"):
        plugins.load_events()


def test_non_event_entry_point_is_rejected(installed):
    installed[plugins.EVENTS_GROUP].append(FakeEntryPoint("toy.in", ToyModule))
    with pytest.raises(plugins.PluginError, match="EventData subclass"):
        plugins.load_events()


# --- tier 0: import paths, no packaging -------------------------------------

TOY_PATH = f"{__name__}:ToyModule"


def test_import_path_loads_without_entry_point(installed):
    assert plugins.load_modules([TOY_PATH]) == {TOY_PATH: ToyModule}


@pytest.mark.parametrize("path", ["no_colon", ":Name", "pkg:", "nope_pkg_xyz:Name"])
def test_bad_import_path_is_a_readable_error(path):
    with pytest.raises(plugins.PluginError):
        plugins.import_object(path)


def test_import_path_must_point_to_a_module(installed):
    with pytest.raises(plugins.PluginError, match="Module subclass"):
        plugins.load_modules([f"{__name__}:ToyEvent"])


def test_parse_selection():
    assert plugins.parse_selection(None) == []
    assert plugins.parse_selection(" stt, rag ,,a.b:C ") == ["stt", "rag", "a.b:C"]


def test_module_config_takes_a_name_or_an_import_path():
    assert ModuleConfig.from_dict({"name": "stt"}).key == "stt"
    assert ModuleConfig.from_dict({"module": TOY_PATH}).key == TOY_PATH
    for raw in ({}, {"name": "stt", "module": TOY_PATH}):
        with pytest.raises(ValueError, match="exactly one"):
            ModuleConfig.from_dict(raw)


def test_session_builds_a_module_given_by_import_path(installed):
    factory = ModuleFactory(handles={})
    for name, cls in plugins.load_modules([TOY_PATH]).items():
        factory.register(name, cls)
    config = ModuleConfig.from_dict({"module": TOY_PATH, "args": {"greeting": "yo"}})
    (module,) = factory.create_from_config("user", {"servo": config})
    assert isinstance(module, ToyModule) and module.greeting == "yo"


def test_client_cannot_use_an_import_path_the_server_did_not_list():
    factory = ModuleFactory(handles={})
    config = ModuleConfig.from_dict({"module": TOY_PATH})
    with pytest.raises(ValueError, match="HURI_MODULES"):
        factory.create_from_config("user", {"servo": config})


# --- the real, installed registry --------------------------------------------


def test_curated_modules_and_events_are_registered_by_huri_core():
    """Needs huri-core installed (`pip install -e .`), like the server does."""
    modules = plugins.load_modules(["vad", "tag", "qag", "eag"])
    assert set(modules) == {"vad", "tag", "qag", "eag"}
    events = plugins.load_events()
    for module in modules.values():
        assert module.input_type in events
        assert module.output_type is None or module.output_type in events


# --- default-deny (HURI_MODULES unset) ----------------------------------------


@pytest.mark.parametrize("selection", [None, [], plugins.parse_selection(" , ")])
def test_unset_allow_list_enables_and_imports_nothing(installed, selection):
    installed[plugins.MODULES_GROUP].append(
        FakeEntryPoint("toy", ImportError("must not be imported"))
    )
    assert plugins.load_modules(selection) == {}


def test_server_without_allow_list_rejects_every_session(monkeypatch):
    """With HURI_MODULES unset, a session asking for an installed module is
    refused, and told why. Builds the registry the way HuRI.__init__ does."""
    monkeypatch.delenv("HURI_MODULES", raising=False)
    enabled = plugins.load_modules(plugins.parse_selection(os.getenv("HURI_MODULES")))
    factory = ModuleFactory(handles={}, installed=plugins.installed_modules())
    for name, cls in enabled.items():
        factory.register(name, cls)

    assert factory.registered() == []
    config = ModuleConfig.from_dict({"name": "tag"})
    with pytest.raises(ValueError, match="installed but not enabled.*HURI_MODULES"):
        factory.create_from_config("user", {"tag": config})


def test_unknown_module_is_not_reported_as_disabled():
    factory = ModuleFactory(handles={}, installed=["tag"])
    with pytest.raises(ValueError, match="Unknown module 'nope'"):
        factory.create("user", "nope")


# --- topic namespaces ----------------------------------------------------------


def test_third_party_cannot_register_a_curated_topic(installed):
    installed[plugins.EVENTS_GROUP].append(
        FakeEntryPoint("huri.motion", ToyEvent, dist="acme-events")
    )
    with pytest.raises(plugins.PluginError, match="reserved.*acme_events.motion"):
        plugins.load_events()


@pytest.mark.parametrize("dist", ["huri-core", "huri-events", "huri_events"])
def test_curated_distributions_own_the_huri_namespace(installed, dist):
    installed[plugins.EVENTS_GROUP].append(
        FakeEntryPoint("huri.motion", ToyEvent, dist=dist)
    )
    assert plugins.load_events() == {"huri.motion": ToyEvent}


@pytest.mark.parametrize("topic", ["motion", "Acme.motion", "acme.", ".motion"])
def test_topic_must_be_namespaced(installed, topic):
    installed[plugins.EVENTS_GROUP].append(FakeEntryPoint(topic, ToyEvent))
    with pytest.raises(plugins.PluginError, match="namespaced"):
        plugins.load_events()


def test_a_package_claims_a_single_namespace(installed):
    installed[plugins.EVENTS_GROUP] += [
        FakeEntryPoint("acme.servo", ToyEvent, dist="acme"),
        FakeEntryPoint("other.servo", OtherToyEvent, dist="acme"),
    ]
    with pytest.raises(plugins.PluginError, match="two namespaces"):
        plugins.load_events()


def test_two_packages_cannot_share_a_namespace(installed):
    installed[plugins.EVENTS_GROUP] += [
        FakeEntryPoint("acme.servo", ToyEvent, dist="acme"),
        FakeEntryPoint("acme.lights", OtherToyEvent, dist="impostor"),
    ]
    with pytest.raises(plugins.PluginError, match="acme.*impostor"):
        plugins.load_events()


def test_replacement_event_coexists_with_the_curated_one(installed):
    installed[plugins.EVENTS_GROUP] += [
        FakeEntryPoint("huri.motion", ToyEvent, dist="huri-core"),
        FakeEntryPoint("acme.motion", OtherToyEvent, dist="acme"),
    ]
    assert plugins.load_events() == {
        "huri.motion": ToyEvent,
        "acme.motion": OtherToyEvent,
    }
