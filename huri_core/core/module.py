from typing import TYPE_CHECKING, Any, AsyncGenerator, Coroutine, Optional, Type

from .events import EventData

if TYPE_CHECKING:
    # Only for annotations: the base classes must import without huri-core[server].
    from ray.serve import handle


class Module:
    """
    Base abstract class for all HuRI processing modules.

    A Module represents a processing node inside the conversational
    event pipeline. Modules consume events of a specific type and
    optionally produce new events.

    Subclasses must implement the `process()` method.

    :input_type:
        Event topic consumed by the module.

    :output_type:
        Event topic produced by the module.
        Can be None for terminal modules.

    :input_event: / :output_event:
        Optional event classes the module expects on those topics. When set,
        the server refuses to start if the installed event package registers
        another class for the topic (a version mismatch).

    :actuation:
        True when the module can drive physical hardware (motors, servos...).
        The server then only loads it if its operator also lists it in
        HURI_ACTUATION_MODULES. Same as `requires.actuation` in a manifest.
    """

    input_type: str
    output_type: Optional[str]
    input_event: Optional[Type[EventData]] = None
    output_event: Optional[Type[EventData]] = None
    actuation: bool = False

    def process(
        self, _
    ) -> Coroutine[Any, Any, EventData | None] | AsyncGenerator[EventData | None, None]:
        raise NotImplementedError


class ModuleWithHandle(Module):
    """
    Base module class with Ray Serve deployment handle support.

    This class extends Module by attaching a Ray Serve deployment handle,
    enabling distributed inference and remote execution.

    :handle:
        Ray Serve deployment handle associated with the module.

    :handle_cls:
        Expected handle implementation type.
    """

    _handle_cls: Type[Any]

    def __init__(self, _handle: "handle.DeploymentHandle", **kwargs):
        super().__init__(**kwargs)
        self._handle = _handle


class ModuleWithId(Module):
    """
    Base module class with user identity support.

    This class extends Module by associating a unique user ID with
    each module instance. Useful for maintaining user-specific state,
    memory, personalization, or contextual processing.

    :user_id:
        Unique identifier associated with the current user.
    """

    def __init__(self, _user_id: str, **kwargs):
        super().__init__(**kwargs)
        self._user_id = _user_id

    def get_user_context(self) -> dict:
        """Override in subclasses to provide user-specific context."""
        return {"_user_id": self._user_id}
