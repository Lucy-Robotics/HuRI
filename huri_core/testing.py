"""Test a module alone: feed events in, check the events out. No Ray, no server.

    from huri_core.testing import LocalHandle, ModuleHarness

    class FakeWhisper:                      # stands in for the Serve deployment
        async def transcribe(self, audio, language):
            return "hello"

    harness = ModuleHarness(STT, handle=LocalHandle(FakeWhisper()), language="en")
    out = harness.run(Voice(frames), Voice(None))   # every event STT emitted
    assert out[-1] == Transcript("hello", True)

`ModuleHarness` builds the module the way the server does (handle and user id
injected), checks each event in and out against the module's declared
`input_event` / `output_event`, and returns what the module emitted.
`LocalHandle` mimics a Ray Serve DeploymentHandle on top of a plain object, so
a module that calls `self._handle.method.remote(...)` or
`self._handle.options(stream=True).method.remote(...)` runs unchanged.

Only needs the base huri-core install: nothing here imports Ray.
"""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Type, Union

from huri_core.core.events import EventData
from huri_core.core.module import Module, ModuleWithHandle, ModuleWithId

__all__ = ["Call", "LocalHandle", "ModuleHarness"]


@dataclass
class Call:
    method: str
    args: Tuple[Any, ...]
    kwargs: Dict[str, Any] = field(default_factory=dict)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _stream(value: Any) -> AsyncIterator[Any]:
    value = await _await(value)
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
    else:
        for item in value:
            yield item


class _LocalMethod:
    def __init__(self, handle: "LocalHandle", name: str):
        self._handle = handle
        self._name = name

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        self._handle.calls.append(Call(self._name, args, kwargs))
        result = getattr(self._handle.target, self._name)(*args, **kwargs)
        if self._handle.streaming:
            return _stream(result)
        return asyncio.ensure_future(_await(result))


class LocalHandle:
    """A DeploymentHandle stand-in calling `target`'s methods in-process.

    `calls` records every `.remote()` call, in order, shared by the handles
    `options()` derives.
    """

    def __init__(self, target: Any, *, streaming: bool = False, _calls=None):
        self.target = target
        self.streaming = streaming
        self.calls: List[Call] = _calls if _calls is not None else []

    def options(self, *, stream: bool = False, **_ignored: Any) -> "LocalHandle":
        return LocalHandle(self.target, streaming=stream, _calls=self.calls)

    def __getattr__(self, name: str) -> _LocalMethod:
        if name.startswith("_") or not hasattr(self.target, name):
            raise AttributeError(
                f"{type(self.target).__name__} has no method {name!r} for the "
                "module to call"
            )
        return _LocalMethod(self, name)


class ModuleHarness:
    """Runs one module in-process.

    :module: a Module subclass (built here, like the server does) or an
        already built instance.
    :handle: what a ModuleWithHandle receives as `_handle`, typically a
        LocalHandle around a fake of its deployment.
    :user_id: what a ModuleWithId receives as `_user_id`.
    :args: the module's own arguments, as in the YAML `args:`.
    """

    def __init__(
        self,
        module: Union[Type[Module], Module],
        *,
        handle: Any = None,
        user_id: str = "test-user",
        **args: Any,
    ):
        if isinstance(module, Module):
            self.module = module
        else:
            kwargs = dict(args)
            if issubclass(module, ModuleWithHandle):
                if handle is None:
                    raise TypeError(
                        f"{module.__name__} needs a deployment handle: pass "
                        "handle=LocalHandle(<fake of its deployment>)"
                    )
                kwargs["_handle"] = handle
            if issubclass(module, ModuleWithId):
                kwargs["_user_id"] = user_id
            self.module = module(**kwargs)

    def _check(self, event: Any, expected: Optional[Type[EventData]], role: str):
        if expected is not None and not isinstance(event, expected):
            name = type(self.module).__name__
            raise AssertionError(
                f"{name} {role} {type(event).__name__}, but declares "
                f"{role.split()[0]}_event = {expected.__name__}"
            )

    async def feed(self, event: EventData) -> List[EventData]:
        """Process one event; returns what the module emitted for it."""

        self._check(event, self.module.input_event, "input got")
        result = self.module.process(event)
        if hasattr(result, "__aiter__"):
            emitted = [item async for item in result if item is not None]
        else:
            value = await result
            emitted = [] if value is None else [value]
        for item in emitted:
            self._check(item, self.module.output_event, "output emitted")
        return emitted

    async def feed_all(self, *events: EventData) -> List[EventData]:
        out: List[EventData] = []
        for event in events:
            out += await self.feed(event)
        return out

    async def finalize(self) -> None:
        """What the server calls when the session ends, if the module has it."""

        fin = getattr(self.module, "finalize", None)
        if fin is not None:
            await fin()

    def run(self, *events: EventData) -> List[EventData]:
        """feed_all() from synchronous code (outside any event loop)."""

        return asyncio.run(self.feed_all(*events))
