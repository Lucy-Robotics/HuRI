from typing import Any, Dict, cast

from omegaconf import OmegaConf
from transformers import PretrainedConfig


class EmageAudioConfig(PretrainedConfig):
    model_type = "emage_audio"

    def __init__(self, config_obj=None, **kwargs):
        if config_obj is not None:
            cfg_dict = cast(
                Dict[str, Any], OmegaConf.to_container(config_obj, resolve=True)
            )
            kwargs.update(cfg_dict)
        super().__init__(**kwargs)


class EmageVQVAEConvConfig(PretrainedConfig):
    model_type = "emage_vqvaeconv"

    def __init__(self, config_obj=None, **kwargs):
        if config_obj is not None:
            cfg_dict = cast(
                Dict[str, Any], OmegaConf.to_container(config_obj, resolve=True)
            )
            kwargs.update(cfg_dict)
        super().__init__(**kwargs)


class EmageVAEConvConfig(PretrainedConfig):
    model_type = "emage_vaeconv"

    def __init__(self, config_obj=None, **kwargs):
        if config_obj is not None:
            cfg_dict = cast(
                Dict[str, Any], OmegaConf.to_container(config_obj, resolve=True)
            )
            kwargs.update(cfg_dict)
        super().__init__(**kwargs)
