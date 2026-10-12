from typing import Any, Dict, Type

from .client import ClientHook, ClientSender


class Interface:
    """This class abstract defining specific Client senders and hooks.

    `self.singletton`: allow hooks to modifies shared ressources,
    and comes from the used interface.

    Class derived from Interface must implement get_senders and get_hooks.
    """

    def __init__(self, singletton: Any):
        self.singletton = singletton

    def get_senders(self) -> Dict[str, Type[ClientSender]]:
        raise NotImplementedError

    def get_hooks(self) -> Dict[str, Type[ClientHook]]:
        raise NotImplementedError
