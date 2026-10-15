"""Every curated topic lives under `huri.`, and the shipped configs use them."""

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.plugins import installed_modules, load_events, load_modules

ROOT = Path(__file__).resolve().parents[2]
CLIENT_CONFIGS = sorted(
    p
    for p in (ROOT / "config").glob("client_*.yaml")
    if p.name != "client_template.yaml"
)


def test_curated_event_topics_are_namespaced():
    events = load_events()
    assert events, "huri-core must be installed (pip install -e .)"
    assert all(topic.startswith("huri.") for topic in events), sorted(events)


def test_curated_module_topics_are_namespaced():
    for name, module in load_modules(installed_modules()).items():
        topics = [module.input_type, module.output_type]
        assert all(t is None or t.startswith("huri.") for t in topics), name


@pytest.mark.parametrize("path", CLIENT_CONFIGS, ids=lambda p: p.name)
def test_shipped_client_configs_use_registered_topics(path):
    raw = OmegaConf.to_container(OmegaConf.load(path), resolve=True)
    config = ClientConfig.from_dict({**raw, "user_id": "test"})
    events = load_events()
    used = config.send + config.subscribe
    assert used and all(topic in events for topic in used), used
