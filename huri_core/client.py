import argparse
import asyncio
from typing import Any, Dict, Tuple

from omegaconf import OmegaConf

from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.user_config import get_or_create_and_save_user_id


def load_raw_client_config(path: str) -> Dict[str, Any]:
    """The YAML, interpolations resolved, with this machine's user_id added."""

    with open(path) as f:
        dict_config = OmegaConf.load(f)
    raw_resolved = OmegaConf.to_container(dict_config, resolve=True)

    if not isinstance(raw_resolved, dict):
        raise RuntimeError("error yaml does not output a dict")

    raw: Dict[str, Any] = {str(k): v for k, v in raw_resolved.items()}
    raw["user_id"] = get_or_create_and_save_user_id(raw.get("user_id_file_path"))
    return raw


def load_client_config(path: str) -> ClientConfig:
    return ClientConfig.from_dict(load_raw_client_config(path))


def load_cli_config(path: str) -> Tuple[ClientConfig, Dict[str, Any]]:
    """The session config and the CLI's own `cli:` device settings."""

    raw = load_raw_client_config(path)
    return ClientConfig.from_dict(raw), dict(raw.get("cli") or {})


async def launch_client():
    parser = argparse.ArgumentParser(description="Client config")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to Client config file (YAML)",
    )

    args = parser.parse_args()
    config, cli = load_cli_config(args.config)

    # Imported here: the CLI needs audio libraries the SDK does not.
    from huri_core.interfaces.cli_interface import run_cli

    await run_cli(config, cli)


if __name__ == "__main__":
    try:
        asyncio.run(launch_client())
    except KeyboardInterrupt:
        pass
