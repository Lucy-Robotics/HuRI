import argparse
import asyncio
from typing import Dict

from omegaconf import OmegaConf

from huri_core.core.client import Client
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.user_config import get_or_create_and_save_user_id


def load_client_config(path: str) -> ClientConfig:
    with open(path) as f:
        dict_config = OmegaConf.load(f)
    raw_resolved = OmegaConf.to_container(dict_config, resolve=True)

    if not isinstance(raw_resolved, Dict):
        raise RuntimeError("error yaml does not output a dict")

    user_id_file_path = raw_resolved.get("user_id_file_path")
    user_id = get_or_create_and_save_user_id(user_id_file_path)
    raw_resolved["user_id"] = user_id

    return ClientConfig.from_dict(raw_resolved)


async def launch_client():
    parser = argparse.ArgumentParser(description="Client config")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to Client config file (YAML)",
    )

    args = parser.parse_args()
    config = load_client_config(args.config)

    await Client(config=config).run()


if __name__ == "__main__":
    try:
        asyncio.run(launch_client())
    except KeyboardInterrupt:
        pass
