"""Compatibility checks between modules, event packages and a session.

Two moments:
  - at startup, `check_modules` verifies that every topic an enabled module
    consumes or produces is registered by an installed event package, with the
    event class the module expects;
  - per session, `check_session` verifies that every module input has a
    producer (another module, or the client) and that every topic the client
    subscribes to is emitted by the pipeline.

Each reports all problems at once, in plain sentences, instead of letting the
pipeline start and silently drop events.
"""

from typing import Dict, Iterable, List, Optional, Type

from .events import EventData
from .module import Module
from .plugins import CURATED_NAMESPACE, topic_namespace


class CompatibilityError(ValueError):
    """Modules, events and session config do not fit together."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


def _qualname(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def missing_package_hint(topic: str) -> str:
    """The event package that most likely provides a topic."""

    namespace = topic_namespace(topic)
    if namespace == CURATED_NAMESPACE:
        return "the curated events (huri-events; provided by huri-core for now)"
    return f"the event package of the {namespace!r} namespace"


def check_modules(
    modules: Dict[str, Type[Module]], events: Dict[str, Type[EventData]]
) -> None:
    """Every enabled module's topics are registered, with the expected class."""

    problems: List[str] = []
    for name, cls in sorted(modules.items()):
        roles = (
            ("consumes", cls.input_type, cls.input_event),
            ("produces", cls.output_type, cls.output_event),
        )
        for role, topic, expected in roles:
            if topic is None:
                continue
            registered: Optional[Type[EventData]] = events.get(topic)
            if registered is None:
                problems.append(
                    f"module {name!r} {role} {topic!r}, but no installed event "
                    f"package registers that topic: install "
                    f"{missing_package_hint(topic)}."
                )
            elif expected is not None and registered is not expected:
                problems.append(
                    f"module {name!r} {role} {topic!r} as {_qualname(expected)}, "
                    f"but the installed event package registers "
                    f"{_qualname(registered)} for it: the module and the event "
                    "package are not compatible versions."
                )
    if problems:
        raise CompatibilityError(problems)


def check_session(
    pipeline: List[Module], sent: Iterable[str], subscribed: Iterable[str]
) -> None:
    """The session's wiring is complete.

    :sent: topics the client publishes to the server.
    :subscribed: topics the client wants to receive.
    """

    sent = set(sent)
    produced = {m.output_type for m in pipeline if m.output_type is not None}
    names = sorted(type(m).__name__ for m in pipeline)
    problems: List[str] = []

    for module in pipeline:
        if module.input_type not in produced and module.input_type not in sent:
            problems.append(
                f"{type(module).__name__} consumes {module.input_type!r}, but "
                "nothing in this session produces it: add a module that emits "
                "it, or have the client send it."
            )

    unknown = sorted({t for t in subscribed if t not in produced})
    if unknown:
        problems.append(
            f"hook topic(s) {unknown} are not emitted by any module in this "
            f"session's pipeline ({names}). Subscribable topics: {sorted(produced)}"
        )
    if problems:
        raise CompatibilityError(problems)
