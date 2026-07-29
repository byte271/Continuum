from __future__ import annotations

import base64
import builtins
import json
import math
import random
from typing import Any

from .errors import ImageError, UnsupportedObjectError
from .resources import PortableFile
from .values import (
    BoundAttrRef,
    BuiltinRef,
    FunctionValue,
    ModuleAttrRef,
    ModuleRef,
    VMIterator,
)

MAX_GRAPH_DEPTH = 500


class GraphEncoder:
    """Safe, explicit graph codec. It never imports or executes encoded types."""

    def __init__(self, max_objects: int = 2_000_000):
        self.nodes: list[dict[str, Any]] = []
        self.memo: dict[int, int] = {}
        self.max_objects = max_objects

    def encode(self, root: Any) -> dict[str, Any]:
        encoded_root = self._value(root)
        return {"codec_version": "0.1", "root": encoded_root, "objects": self.nodes}

    def _reference(self, value: Any, kind: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        identity = id(value)
        if identity in self.memo:
            return {"t": "ref", "id": self.memo[identity]}, {}
        if len(self.nodes) >= self.max_objects:
            raise UnsupportedObjectError("live object graph exceeds the object limit")
        node_id = len(self.nodes)
        self.memo[identity] = node_id
        node: dict[str, Any] = {"id": node_id, "kind": kind}
        self.nodes.append(node)
        return {"t": "ref", "id": node_id}, node

    def _value(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {"t": "none"}
        if isinstance(value, bool):
            return {"t": "bool", "v": value}
        if isinstance(value, int):
            return {"t": "int", "v": str(value)}
        if isinstance(value, float):
            if not math.isfinite(value):
                return {"t": "float", "v": value.hex()}
            return {"t": "float", "v": value.hex()}
        if isinstance(value, str):
            return {"t": "str", "v": value}
        if isinstance(value, bytes):
            return {"t": "bytes", "v": base64.b64encode(value).decode("ascii")}

        if isinstance(value, list):
            ref, node = self._reference(value, "list")
            if node:
                node["items"] = [self._value(item) for item in value]
            return ref
        if isinstance(value, dict):
            ref, node = self._reference(value, "dict")
            if node:
                node["items"] = [
                    [self._value(key), self._value(item)] for key, item in value.items()
                ]
            return ref
        if isinstance(value, tuple):
            ref, node = self._reference(value, "tuple")
            if node:
                node["items"] = [self._value(item) for item in value]
            return ref
        if isinstance(value, set):
            ref, node = self._reference(value, "set")
            if node:
                node["items"] = [
                    self._value(item)
                    for item in sorted(value, key=self._stable_set_key)
                ]
            return ref
        if isinstance(value, frozenset):
            ref, node = self._reference(value, "frozenset")
            if node:
                node["items"] = [
                    self._value(item)
                    for item in sorted(value, key=self._stable_set_key)
                ]
            return ref
        if isinstance(value, bytearray):
            ref, node = self._reference(value, "bytearray")
            if node:
                node["value"] = base64.b64encode(bytes(value)).decode("ascii")
            return ref
        if isinstance(value, range):
            ref, node = self._reference(value, "range")
            if node:
                node.update({"start": value.start, "stop": value.stop, "step": value.step})
            return ref
        if isinstance(value, random.Random):
            ref, node = self._reference(value, "random")
            if node:
                node["state"] = self._value(value.getstate())
            return ref
        if isinstance(value, FunctionValue):
            ref, node = self._reference(value, "function")
            if node:
                node["function_id"] = value.function_id
            return ref
        if isinstance(value, BuiltinRef):
            ref, node = self._reference(value, "builtin_ref")
            if node:
                node["name"] = value.name
            return ref
        if isinstance(value, ModuleRef):
            ref, node = self._reference(value, "module_ref")
            if node:
                node["name"] = value.name
            return ref
        if isinstance(value, ModuleAttrRef):
            ref, node = self._reference(value, "module_attr_ref")
            if node:
                node.update({"module": value.module, "attr": value.attr})
            return ref
        if isinstance(value, BoundAttrRef):
            ref, node = self._reference(value, "bound_attr_ref")
            if node:
                node.update({"receiver": self._value(value.receiver), "attr": value.attr})
            return ref
        if isinstance(value, VMIterator):
            ref, node = self._reference(value, "iterator")
            if node:
                node.update(
                    {
                        "iterable": self._value(value.iterable),
                        "index": value.index,
                        "dict_keys": self._value(value.dict_keys),
                    }
                )
            return ref
        if isinstance(value, PortableFile):
            ref, node = self._reference(value, "resource_ref")
            if node:
                node["resource_id"] = value.resource_id
            return ref
        if isinstance(value, BaseException):
            module = type(value).__module__
            if module != "builtins":
                raise UnsupportedObjectError(
                    f"unsupported live exception type: {module}.{type(value).__name__}"
                )
            ref, node = self._reference(value, "exception")
            if node:
                node.update(
                    {"name": type(value).__name__, "args": self._value(value.args)}
                )
            return ref
        raise UnsupportedObjectError(
            f"unsupported live object: {type(value).__module__}.{type(value).__qualname__}"
        )

    @staticmethod
    def _stable_set_key(value: Any) -> str:
        try:
            document = GraphEncoder().encode(value)
            return json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except RecursionError as exc:
            raise UnsupportedObjectError(
                "set element exceeds the portable graph nesting limit"
            ) from exc


class GraphDecoder:
    def __init__(
        self,
        document: dict[str, Any],
        resources: dict[str, PortableFile] | None = None,
        max_objects: int = 2_000_000,
    ):
        if not isinstance(document, dict) or document.get("codec_version") != "0.1":
            raise ImageError("unsupported heap codec version")
        self.nodes = document.get("objects")
        if not isinstance(self.nodes, list) or len(self.nodes) > max_objects:
            raise ImageError("invalid or excessive heap object table")
        self.resources = resources or {}
        self.memo: dict[int, Any] = {}
        self.in_progress: set[int] = set()
        self._validate_ids()
        self.root_spec = document.get("root")

    def decode(self) -> Any:
        return self._value(self.root_spec)

    def _validate_ids(self) -> None:
        for index, node in enumerate(self.nodes):
            if not isinstance(node, dict) or node.get("id") != index:
                raise ImageError("heap object identifiers are not canonical")

    def _value(self, spec: Any, depth: int = 0) -> Any:
        if depth > MAX_GRAPH_DEPTH:
            raise ImageError("heap graph nesting limit exceeded")
        if not isinstance(spec, dict) or not isinstance(spec.get("t"), str):
            raise ImageError("invalid heap value")
        kind = spec["t"]
        if kind == "none":
            return None
        if kind == "bool":
            if type(spec.get("v")) is not bool:
                raise ImageError("invalid boolean in heap")
            return spec["v"]
        if kind == "int":
            try:
                return int(spec["v"])
            except (TypeError, ValueError) as exc:
                raise ImageError("invalid integer in heap") from exc
        if kind == "float":
            try:
                return float.fromhex(spec["v"])
            except (TypeError, ValueError) as exc:
                raise ImageError("invalid float in heap") from exc
        if kind == "str":
            if not isinstance(spec.get("v"), str):
                raise ImageError("invalid string in heap")
            return spec["v"]
        if kind == "bytes":
            try:
                return base64.b64decode(spec["v"], validate=True)
            except Exception as exc:
                raise ImageError("invalid bytes in heap") from exc
        if kind == "ref":
            return self._object(spec.get("id"), depth + 1)
        raise ImageError(f"unknown heap value type: {kind!r}")

    def _object(self, node_id: Any, depth: int) -> Any:
        if depth > MAX_GRAPH_DEPTH:
            raise ImageError("heap graph nesting limit exceeded")
        if not isinstance(node_id, int) or not 0 <= node_id < len(self.nodes):
            raise ImageError("invalid heap reference")
        if node_id in self.memo:
            return self.memo[node_id]
        if node_id in self.in_progress:
            raise ImageError(
                "cycle through an immutable or wrapper object is unsupported"
            )
        node = self.nodes[node_id]
        kind = node.get("kind")

        if kind == "list":
            result: Any = []
            self.memo[node_id] = result
            result.extend(self._value(item, depth + 1) for item in self._items(node))
            return result
        if kind == "dict":
            result = {}
            self.memo[node_id] = result
            for pair in self._items(node):
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ImageError("invalid dictionary record")
                try:
                    key = self._value(pair[0], depth + 1)
                    if key in result:
                        raise ImageError("duplicate dictionary key in heap")
                    result[key] = self._value(pair[1], depth + 1)
                except TypeError as exc:
                    raise ImageError("unhashable dictionary key in heap") from exc
            return result
        if kind == "set":
            result = set()
            self.memo[node_id] = result
            try:
                result.update(
                    self._value(item, depth + 1) for item in self._items(node)
                )
            except TypeError as exc:
                raise ImageError("unhashable set item in heap") from exc
            return result
        if kind == "bytearray":
            try:
                result = bytearray(base64.b64decode(node["value"], validate=True))
            except Exception as exc:
                raise ImageError("invalid bytearray record") from exc
            self.memo[node_id] = result
            return result
        if kind == "random":
            result = random.Random()
            self.memo[node_id] = result
            try:
                result.setstate(self._value(node.get("state"), depth + 1))
            except (TypeError, ValueError) as exc:
                raise ImageError("invalid random generator state") from exc
            return result
        if kind == "iterator":
            result = VMIterator(None, 0)
            self.memo[node_id] = result
            result.iterable = self._value(node.get("iterable"), depth + 1)
            result.dict_keys = self._value(node.get("dict_keys"), depth + 1)
            if result.dict_keys is not None and not isinstance(
                result.dict_keys, tuple
            ):
                raise ImageError("invalid dictionary iterator key snapshot")
            index = node.get("index")
            if not isinstance(index, int) or index < 0:
                raise ImageError("invalid iterator index")
            result.index = index
            return result
        if kind == "resource_ref":
            resource_id = node.get("resource_id")
            if resource_id not in self.resources:
                raise ImageError(f"missing rebound resource {resource_id!r}")
            result = self.resources[resource_id]
            self.memo[node_id] = result
            return result

        self.in_progress.add(node_id)
        try:
            if kind == "tuple":
                result = tuple(
                    self._value(item, depth + 1) for item in self._items(node)
                )
            elif kind == "frozenset":
                result = frozenset(
                    self._value(item, depth + 1) for item in self._items(node)
                )
            elif kind == "range":
                try:
                    result = range(
                        self._plain_int(node, "start"),
                        self._plain_int(node, "stop"),
                        self._plain_int(node, "step"),
                    )
                except ValueError as exc:
                    raise ImageError("invalid range in heap") from exc
            elif kind == "function":
                result = FunctionValue(self._plain_str(node, "function_id"))
            elif kind == "builtin_ref":
                result = BuiltinRef(self._plain_str(node, "name"))
            elif kind == "module_ref":
                result = ModuleRef(self._plain_str(node, "name"))
            elif kind == "module_attr_ref":
                result = ModuleAttrRef(
                    self._plain_str(node, "module"), self._plain_str(node, "attr")
                )
            elif kind == "bound_attr_ref":
                result = BoundAttrRef(
                    self._value(node.get("receiver"), depth + 1),
                    self._plain_str(node, "attr"),
                )
            elif kind == "exception":
                name = self._plain_str(node, "name")
                exception_type = getattr(builtins, name, None)
                if (
                    not isinstance(exception_type, type)
                    or not issubclass(exception_type, BaseException)
                ):
                    raise ImageError(f"unsupported exception record: {name}")
                args = self._value(node.get("args"), depth + 1)
                if not isinstance(args, tuple):
                    raise ImageError("exception args are not a tuple")
                result = exception_type(*args)
            else:
                raise ImageError(f"unknown heap object kind: {kind!r}")
            self.memo[node_id] = result
            return result
        finally:
            self.in_progress.remove(node_id)

    @staticmethod
    def _items(node: dict[str, Any]) -> list[Any]:
        items = node.get("items")
        if not isinstance(items, list):
            raise ImageError("invalid heap collection")
        return items

    @staticmethod
    def _plain_str(node: dict[str, Any], key: str) -> str:
        value = node.get(key)
        if not isinstance(value, str):
            raise ImageError(f"invalid {key} in heap record")
        return value

    @staticmethod
    def _plain_int(node: dict[str, Any], key: str) -> int:
        value = node.get(key)
        if not isinstance(value, int):
            raise ImageError(f"invalid {key} in heap record")
        return value


def encode_graph(root: Any) -> dict[str, Any]:
    try:
        return GraphEncoder().encode(root)
    except RecursionError as exc:
        raise UnsupportedObjectError(
            "live object graph exceeds the portable nesting limit"
        ) from exc


def decode_graph(
    document: dict[str, Any], resources: dict[str, PortableFile] | None = None
) -> Any:
    try:
        return GraphDecoder(document, resources).decode()
    except RecursionError as exc:
        raise ImageError("heap graph nesting limit exceeded") from exc
