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

    def to_dict(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {"args": dict(self.args)}
        if self.module is not None:
            raw["module"] = self.module
        else:
            raw["name"] = self.name
        return raw


@dataclass
class ClientConfig:
    """A client session, as written in a client YAML and sent to the server as
    the session handshake.

    :modules: the session's pipeline.
    :subscribe: topics the client receives.
    :send: topics the client publishes (module inputs it provides).
    """

    user_id: str
    modules: Dict[str, ModuleConfig]
    subscribe: List[str]
    send: List[str]
    huri_url: str = ""

    LEGACY_KEYS = ("interface_path", "senders", "hooks")

    @classmethod
    def from_dict(cls, raw: Dict) -> "ClientConfig":
        legacy = [k for k in cls.LEGACY_KEYS if k in raw]
        if legacy:
            raise ValueError(
                f"client config uses the removed keys {legacy}: list the topics to "
                "receive under `subscribe:` and the topics sent under `send:` "
                "instead (see config/client_template.yaml in "
                "https://github.com/Lucy-Robotics/huri-modules)"
            )
        modules = {
            module_id: ModuleConfig.from_dict(mod_raw)
            for module_id, mod_raw in raw.get("modules", {}).items()
        }
        return cls(
            user_id=raw["user_id"],
            huri_url=raw.get("huri_url", ""),
            modules=modules,
            subscribe=list(raw.get("subscribe", [])),
            send=list(raw.get("send", [])),
        )
