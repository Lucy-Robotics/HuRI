from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Set

from .files import load_mapping

PRESETS_PATH = Path(__file__).parent / "presets.yaml"


@dataclass(frozen=True)
class InboundRule:
    """Limits on one topic the browser is allowed to publish."""

    max_per_minute: int
    max_chars: int | None = None


@dataclass(frozen=True)
class Preset:
    """A pipeline a visitor can pick. Only its id ever crosses the wire."""

    id: str
    label: str
    description: str
    inbound: Mapping[str, InboundRule]
    outbound: List[str]
    modules: Mapping[str, Any]
    # Module -> the outbound topics only it produces. Dropped, topics included,
    # when the HuRI server cannot build it (e.g. mov without EMAGE).
    optional: Mapping[str, List[str]] = field(default_factory=dict)

    def public(self) -> Dict[str, str]:
        return {"id": self.id, "label": self.label, "description": self.description}

    def missing_optional(self, server_modules: Iterable[str]) -> Set[str]:
        """Optional modules of this preset the server does not have."""
        available = set(server_modules)
        return {name for name in self.optional if name not in available}

    def without(self, names: Iterable[str]) -> "Preset":
        names = set(names)
        topics = {topic for name in names for topic in self.optional.get(name, [])}
        return replace(
            self,
            modules={k: v for k, v in self.modules.items() if k not in names},
            outbound=[t for t in self.outbound if t not in topics],
            optional={k: v for k, v in self.optional.items() if k not in names},
        )

    def handshake(self, user_id: str, huri_url: str) -> Dict[str, Any]:
        """The ``ClientConfig`` HuRI expects as the first message of a session."""
        return {
            "user_id": user_id,
            "huri_url": huri_url,
            "interface_path": "src.playground.gateway",
            "hooks": {
                topic: {"name": topic, "topics": [topic], "args": {}}
                for topic in self.outbound
            },
            "senders": {
                topic: {"name": topic, "topic": topic, "args": {}}
                for topic in self.inbound
            },
            "modules": dict(self.modules),
        }


def load_presets(path: Path = PRESETS_PATH) -> Dict[str, Preset]:
    raw = load_mapping(path)
    presets = {}

    for preset_id, entry in raw.items():
        preset = Preset(
            id=preset_id,
            label=entry["label"],
            description=entry["description"],
            inbound={
                topic: InboundRule(**rule) for topic, rule in entry["inbound"].items()
            },
            outbound=list(entry["outbound"]),
            modules=entry["modules"],
            optional={
                name: list(topics) for name, topics in entry.get("optional", {}).items()
            },
        )
        for name, topics in preset.optional.items():
            if name not in preset.modules or not set(topics) <= set(preset.outbound):
                raise ValueError(
                    f"{path}: preset {preset_id!r}: optional {name!r} must be one "
                    "of its modules, listing only its outbound topics"
                )
        presets[preset_id] = preset

    return presets
