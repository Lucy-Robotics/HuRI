import logging
from dataclasses import asdict, dataclass
from typing import Any, Generic, Mapping, TypeVar

logger = logging.getLogger("ray.serve")

WireT = TypeVar("WireT", Mapping[str, Any], bytes)


@dataclass
class EventData(Generic[WireT]):
    """An event data must be derived from this class, and use @dataclass decorator.
    Or they can be bytes."""

    @classmethod
    def from_wire(cls, data: WireT) -> "EventData[WireT]":
        raise NotImplementedError

    def to_wire(self) -> WireT:
        raise NotImplementedError

    def summarize(self) -> str:
        cls = type(self).__name__
        return f"{cls}({self!r})"


@dataclass
class JsonEvent(EventData[Mapping[str, Any]]):
    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "JsonEvent":
        return cls(**data)

    def to_wire(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass
class BytesEvent(EventData[bytes]):
    data: bytes

    @classmethod
    def from_wire(cls, data: bytes) -> "BytesEvent":
        return cls(data=data)

    def to_wire(self) -> bytes:
        return self.data

    def summarize(self) -> str:
        cls = type(self).__name__
        return f"{cls}(len:{len(self.data)})"
