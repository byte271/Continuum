from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FunctionValue:
    function_id: str


@dataclass(frozen=True)
class BuiltinRef:
    name: str


@dataclass(frozen=True)
class ModuleRef:
    name: str


@dataclass(frozen=True)
class ModuleAttrRef:
    module: str
    attr: str


@dataclass(frozen=True)
class BoundAttrRef:
    receiver: Any
    attr: str


@dataclass
class VMIterator:
    iterable: Any
    index: int = 0
    dict_keys: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.iterable, dict) and self.dict_keys is None:
            self.dict_keys = tuple(self.iterable)

    def next_value(self) -> Any:
        if isinstance(self.iterable, dict):
            current_keys = tuple(self.iterable)
            if current_keys != self.dict_keys:
                raise RuntimeError("dictionary keys changed during iteration")
            if self.index >= len(current_keys):
                raise StopIteration
            value = current_keys[self.index]
        else:
            if self.index >= len(self.iterable):
                raise StopIteration
            value = self.iterable[self.index]
        self.index += 1
        return value
