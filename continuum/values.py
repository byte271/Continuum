from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FunctionValue:
    function_id: str
    defaults: tuple[Any, ...] = ()
    # Keyword-only defaults, aligned with the definition's kw_default_names.
    # Held separately because they bind by name, not by position.
    kw_defaults: tuple[Any, ...] = ()
    # Captured cells, aligned with the definition's freevars. These are the
    # same Cell objects the enclosing frame holds, not copies.
    closure: tuple["Cell", ...] = ()


class Empty:
    """Marker for a cell that has never been assigned."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<empty cell>"


EMPTY = Empty()


@dataclass(eq=False)
class Cell:
    """One closed-over binding, shared by every function that captures it.

    Mutable and compared by identity, so the graph codec preserves sharing the
    same way it does for a list: two functions that close over one variable
    still share one cell after an image round trip.
    """

    value: Any = EMPTY

    def get(self, name: str) -> Any:
        if isinstance(self.value, Empty):
            raise NameError(
                f"free variable {name!r} referenced before assignment in "
                "enclosing scope"
            )
        return self.value

    def set(self, value: Any) -> None:
        self.value = value

    def is_empty(self) -> bool:
        return isinstance(self.value, Empty)


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
