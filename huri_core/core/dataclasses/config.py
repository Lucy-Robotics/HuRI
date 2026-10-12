from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class ModuleConfig:
    """A module of the session's pipeline, given either by registered `name`
    (an installed `huri.modules` entry point) or by `module` import path
    (`my_pkg.servo:Servo`, which the server must list in HURI_MODULES)."""

    name: Optional[str]
    args: Mapping[str, Any]
    module: Optional[str] = None

    @property
    def key(self) -> str:
        """What the server's module registry is looked up with."""
        return self.module or self.name or ""

    @classmethod
    def from_dict(self, raw: dict) -> "ModuleConfig":
        name, module = raw.get("name"), raw.get("module")
        if (name is None) == (module is None):
            raise ValueError(
                f"a module entry needs exactly one of 'name' or 'module': {raw!r}"
            )
        return self(name=name, args=raw.get("args", {}), module=module)


@dataclass
class ClientHookConfig:
    name: str
    topics: List[str]
    args: Mapping[str, Any]

    @classmethod
    def from_dict(self, raw: dict) -> "ClientHookConfig":
        return self(
            name=raw["name"],
            topics=raw["topics"],
            args=raw.get("args", {}),
        )


@dataclass
class ClientSenderConfig:
    name: str
    topic: str
    args: Mapping[str, Any]

    @classmethod
    def from_dict(self, raw: dict) -> "ClientSenderConfig":
        return self(
            name=raw["name"],
            topic=raw["topic"],
            args=raw.get("args", {}),
        )


@dataclass
class ClientConfig:
    user_id: str
    huri_url: str
    interface_path: str
    hooks: Dict[str, ClientHookConfig]
    senders: Dict[str, ClientSenderConfig]
    modules: Dict[str, ModuleConfig]

    @classmethod
    def from_dict(cls, raw: Dict) -> "ClientConfig":
        hooks = {
            hook_id: ClientHookConfig.from_dict(hok_raw)
            for hook_id, hok_raw in raw.get("hooks", {}).items()
        }
        senders = {
            sender_id: ClientSenderConfig.from_dict(snd_raw)
            for sender_id, snd_raw in raw.get("senders", {}).items()
        }
        modules = {
            module_id: ModuleConfig.from_dict(mod_raw)
            for module_id, mod_raw in raw.get("modules", {}).items()
        }
        return cls(
            user_id=raw["user_id"],
            huri_url=raw["huri_url"],
            interface_path=raw["interface_path"],
            hooks=hooks,
            senders=senders,
            modules=modules,
        )
