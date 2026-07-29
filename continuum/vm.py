from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import math
import operator
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from . import IR_VERSION
from .errors import ExecutionError, FrozenExecution, ImageError
from .resources import PortableFile, ResourceManager
from .values import (
    BoundAttrRef,
    BuiltinRef,
    FunctionValue,
    ModuleAttrRef,
    ModuleRef,
    VMIterator,
)


ALLOWED_MODULES = {"hashlib", "json", "math", "random"}
ALLOWED_MODULE_ATTRS = {
    "hashlib": {"md5", "new", "sha1", "sha224", "sha256", "sha384", "sha512"},
    "json": {"dumps", "loads"},
    "math": {
        "acos",
        "asin",
        "atan",
        "atan2",
        "ceil",
        "comb",
        "copysign",
        "cos",
        "degrees",
        "dist",
        "exp",
        "fabs",
        "factorial",
        "floor",
        "fmod",
        "fsum",
        "gcd",
        "hypot",
        "isclose",
        "isfinite",
        "isinf",
        "isnan",
        "isqrt",
        "lcm",
        "ldexp",
        "log",
        "log10",
        "log2",
        "perm",
        "pow",
        "radians",
        "remainder",
        "sin",
        "sqrt",
        "tan",
        "trunc",
    },
    "random": {
        "Random",
        "choice",
        "getrandbits",
        "getstate",
        "randint",
        "random",
        "randrange",
        "seed",
        "setstate",
        "uniform",
    },
}
ALLOWED_BUILTINS = {
    "AssertionError",
    "Exception",
    "RuntimeError",
    "TypeError",
    "ValueError",
    "abs",
    "bool",
    "dict",
    "float",
    "format",
    "int",
    "len",
    "list",
    "max",
    "min",
    "open",
    "print",
    "range",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
}
ALLOWED_METHODS: dict[type, set[str]] = {
    str: {
        "encode",
        "endswith",
        "join",
        "lower",
        "replace",
        "split",
        "startswith",
        "strip",
        "upper",
    },
    bytes: {"decode", "hex"},
    list: {"append", "clear", "count", "extend", "index", "insert", "pop", "reverse"},
    dict: {"clear", "copy", "get", "items", "keys", "pop", "setdefault", "update", "values"},
    set: {"add", "clear", "discard", "remove"},
    random.Random: {
        "choice",
        "getrandbits",
        "getstate",
        "randint",
        "random",
        "randrange",
        "setstate",
        "uniform",
    },
    PortableFile: {"close", "read", "readline", "readlines", "seek", "tell"},
    type(hashlib.sha256()): {"copy", "digest", "hexdigest", "update"},
}

BINARY_OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "truediv": operator.truediv,
    "floordiv": operator.floordiv,
    "mod": operator.mod,
    "pow": operator.pow,
    "lshift": operator.lshift,
    "rshift": operator.rshift,
    "or": operator.or_,
    "xor": operator.xor,
    "and": operator.and_,
}
UNARY_OPERATORS: dict[str, Callable[[Any], Any]] = {
    "pos": operator.pos,
    "neg": operator.neg,
    "not": operator.not_,
    "invert": operator.invert,
}
COMPARE_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "is": operator.is_,
    "is_not": operator.is_not,
    "in": lambda left, right: left in right,
    "not_in": lambda left, right: left not in right,
}


@dataclass
class Frame:
    function_id: str
    pc: int
    locals: dict[str, Any]
    stack: list[Any] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    finally_reasons: list[dict[str, Any]] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        return {
            "function_id": self.function_id,
            "pc": self.pc,
            "locals": self.locals,
            "stack": self.stack,
            "blocks": self.blocks,
            "finally_reasons": self.finally_reasons,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Frame":
        return cls(
            function_id=state["function_id"],
            pc=state["pc"],
            locals=state["locals"],
            stack=state["stack"],
            blocks=state["blocks"],
            finally_reasons=state["finally_reasons"],
        )


class VirtualMachine:
    def __init__(
        self,
        ir: dict[str, Any],
        argv: list[str],
        source_path: str,
        resource_policy: str = "strict",
        safe_point_callback: Callable[["VirtualMachine"], None] | None = None,
    ):
        validate_ir(ir)
        self.ir = ir
        self.argv = list(argv)
        self.source_path = source_path
        self.resources = ResourceManager(resource_policy)
        self.safe_point_callback = safe_point_callback
        self.globals: dict[str, Any] = {
            "__name__": "__main__",
            "__file__": source_path,
            "__args__": list(argv),
        }
        self.frames = [Frame(ir["entry_function"], 0, self.globals)]
        self.completed = False
        self.result: Any = None
        self.instructions_executed = 0
        self.safe_points_executed = 0
        self._prepare_execution()

    @classmethod
    def restore(
        cls,
        ir: dict[str, Any],
        state: dict[str, Any],
        resources: ResourceManager,
        safe_point_callback: Callable[["VirtualMachine"], None] | None = None,
    ) -> "VirtualMachine":
        validate_ir(ir)
        required = {
            "globals",
            "frames",
            "argv",
            "source_path",
            "instructions_executed",
            "safe_points_executed",
            "module_random_state",
        }
        if not isinstance(state, dict) or required - set(state):
            raise ImageError("runtime state is incomplete")
        vm = cls.__new__(cls)
        vm.ir = ir
        vm.argv = state["argv"]
        vm.source_path = state["source_path"]
        vm.resources = resources
        vm.safe_point_callback = safe_point_callback
        vm.globals = state["globals"]
        vm.frames = [Frame.from_state(item) for item in state["frames"]]
        vm.completed = False
        vm.result = None
        vm.instructions_executed = state["instructions_executed"]
        vm.safe_points_executed = state["safe_points_executed"]
        random.setstate(state["module_random_state"])
        vm._validate_state()
        vm._prepare_execution()
        return vm

    def state_root(self) -> dict[str, Any]:
        return {
            "globals": self.globals,
            "frames": [frame.to_state() for frame in self.frames],
            "argv": self.argv,
            "source_path": self.source_path,
            "instructions_executed": self.instructions_executed,
            "safe_points_executed": self.safe_points_executed,
            "module_random_state": random.getstate(),
        }

    def run(self) -> Any:
        # This is the hot path. It intentionally inlines `step` so every IR
        # instruction does not pay for a second Python method call and repeated
        # decoding of the instruction dictionary. `step` remains available for
        # debuggers, tests, and controlled single-instruction execution.
        frames = self.frames
        decoded_code = self._decoded_code
        execute = self._execute
        handle_exception = self._handle_exception
        while frames:
            frame = frames[-1]
            code = decoded_code[frame.function_id]
            if not 0 <= frame.pc < len(code):
                raise ExecutionError(
                    f"instruction pointer outside {frame.function_id}: {frame.pc}"
                )
            op, arg, line = code[frame.pc]
            self.instructions_executed += 1
            try:
                execute(frame, op, arg)
            except (FrozenExecution, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                if not handle_exception(exc):
                    raise ExecutionError(
                        f"unhandled {type(exc).__name__} at "
                        f"{self.ir['source_name']}:{line}: {exc}"
                    ) from exc
        self.completed = True
        return self.result

    def step(self) -> None:
        frame = self.frames[-1]
        code = self._decoded_code[frame.function_id]
        if not 0 <= frame.pc < len(code):
            raise ExecutionError(
                f"instruction pointer outside {frame.function_id}: {frame.pc}"
            )
        op, arg, line = code[frame.pc]
        self.instructions_executed += 1
        try:
            self._execute(frame, op, arg)
        except (FrozenExecution, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            if not self._handle_exception(exc):
                raise ExecutionError(
                    f"unhandled {type(exc).__name__} at "
                    f"{self.ir['source_name']}:{line}: {exc}"
                ) from exc

    def _prepare_execution(self) -> None:
        self._decoded_code = {
            function_id: tuple(
                (
                    instruction["op"],
                    instruction.get("arg"),
                    instruction["line"],
                )
                for instruction in definition["code"]
            )
            for function_id, definition in self.ir["functions"].items()
        }

    def _execute(self, frame: Frame, op: str, arg: Any) -> None:
        stack = frame.stack
        if op == "CONST":
            stack.append(self._constant(arg))
            frame.pc += 1
        elif op == "LOAD_NAME":
            if arg in frame.locals:
                stack.append(frame.locals[arg])
            elif (
                frame.function_id != "__module__"
                and arg
                in self.ir["functions"][frame.function_id]["local_names"]
            ):
                raise UnboundLocalError(
                    f"local variable {arg!r} referenced before assignment"
                )
            elif arg in self.globals:
                stack.append(self.globals[arg])
            elif arg in ALLOWED_BUILTINS:
                stack.append(BuiltinRef(arg))
            else:
                raise NameError(arg)
            frame.pc += 1
        elif op == "STORE_NAME":
            self._require_stack(stack, 1, op)
            target = self.globals if frame.function_id == "__module__" else frame.locals
            target[arg] = stack.pop()
            frame.pc += 1
        elif op == "POP_TOP":
            self._require_stack(stack, 1, op)
            stack.pop()
            frame.pc += 1
        elif op == "BUILD_LIST":
            stack.append(self._pop_many(stack, arg))
            frame.pc += 1
        elif op == "BUILD_TUPLE":
            stack.append(tuple(self._pop_many(stack, arg)))
            frame.pc += 1
        elif op == "BUILD_SET":
            stack.append(set(self._pop_many(stack, arg)))
            frame.pc += 1
        elif op == "BUILD_DICT":
            values = self._pop_many(stack, arg * 2)
            stack.append({values[i]: values[i + 1] for i in range(0, len(values), 2)})
            frame.pc += 1
        elif op == "BUILD_SLICE":
            values = self._pop_many(stack, arg)
            stack.append(slice(*values))
            frame.pc += 1
        elif op == "BUILD_STRING":
            stack.append("".join(str(value) for value in self._pop_many(stack, arg)))
            frame.pc += 1
        elif op == "FORMAT_VALUE":
            self._require_stack(stack, 2, op)
            spec = stack.pop()
            value = stack.pop()
            stack.append(format(value, spec))
            frame.pc += 1
        elif op == "BINARY":
            self._require_stack(stack, 2, op)
            right = stack.pop()
            left = stack.pop()
            stack.append(BINARY_OPERATORS[arg](left, right))
            frame.pc += 1
        elif op == "UNARY":
            self._require_stack(stack, 1, op)
            stack.append(UNARY_OPERATORS[arg](stack.pop()))
            frame.pc += 1
        elif op == "COMPARE":
            self._require_stack(stack, 2, op)
            right = stack.pop()
            left = stack.pop()
            stack.append(COMPARE_OPERATORS[arg](left, right))
            frame.pc += 1
        elif op == "JUMP":
            frame.pc = arg
        elif op == "JUMP_IF_FALSE":
            self._require_stack(stack, 1, op)
            frame.pc = arg if not stack.pop() else frame.pc + 1
        elif op == "JUMP_IF_FALSE_OR_POP":
            self._require_stack(stack, 1, op)
            if not stack[-1]:
                frame.pc = arg
            else:
                stack.pop()
                frame.pc += 1
        elif op == "JUMP_IF_TRUE_OR_POP":
            self._require_stack(stack, 1, op)
            if stack[-1]:
                frame.pc = arg
            else:
                stack.pop()
                frame.pc += 1
        elif op == "MAKE_FUNCTION":
            stack.append(FunctionValue(arg))
            frame.pc += 1
        elif op == "CALL":
            keyword_names = arg["keywords"]
            keyword_values = self._pop_many(stack, len(keyword_names))
            positional = self._pop_many(stack, arg["positional"])
            self._require_stack(stack, 1, op)
            callable_value = stack.pop()
            kwargs = dict(zip(keyword_names, keyword_values))
            if isinstance(callable_value, FunctionValue):
                frame.pc += 1
                self._call_function(callable_value, positional, kwargs)
            else:
                result = self._call_host(callable_value, positional, kwargs)
                stack.append(result)
                frame.pc += 1
        elif op == "RETURN":
            self._require_stack(stack, 1, op)
            value = stack.pop()
            self.frames.pop()
            if self.frames:
                self.frames[-1].stack.append(value)
            else:
                self.result = value
        elif op == "IMPORT_MODULE":
            if arg not in ALLOWED_MODULES:
                raise ImportError(f"module {arg!r} is not in the v0.1 allowlist")
            stack.append(ModuleRef(arg))
            frame.pc += 1
        elif op == "LOAD_ATTR":
            self._require_stack(stack, 1, op)
            receiver = stack.pop()
            if isinstance(receiver, ModuleRef):
                stack.append(ModuleAttrRef(receiver.name, arg))
            else:
                stack.append(BoundAttrRef(receiver, arg))
            frame.pc += 1
        elif op == "STORE_ATTR":
            self._require_stack(stack, 2, op)
            value = stack.pop()
            receiver = stack.pop()
            setattr(receiver, arg, value)
            frame.pc += 1
        elif op == "LOAD_SUBSCR":
            self._require_stack(stack, 2, op)
            index = stack.pop()
            container = stack.pop()
            stack.append(container[index])
            frame.pc += 1
        elif op == "STORE_SUBSCR_VALUE_FIRST":
            self._require_stack(stack, 3, op)
            index = stack.pop()
            container = stack.pop()
            value = stack.pop()
            container[index] = value
            frame.pc += 1
        elif op == "UNPACK":
            self._require_stack(stack, 1, op)
            values = list(stack.pop())
            if len(values) != arg:
                raise ValueError(f"expected {arg} values, got {len(values)}")
            stack.extend(reversed(values))
            frame.pc += 1
        elif op == "GET_ITER":
            self._require_stack(stack, 1, op)
            iterable = stack.pop()
            if not isinstance(iterable, (range, list, tuple, str, bytes, dict)):
                raise TypeError(
                    f"v0.1 cannot checkpoint iteration over {type(iterable).__name__}"
                )
            stack.append(VMIterator(iterable))
            frame.pc += 1
        elif op == "FOR_ITER":
            self._require_stack(stack, 1, op)
            iterator = stack[-1]
            if not isinstance(iterator, VMIterator):
                raise TypeError("FOR_ITER operand is not a portable iterator")
            try:
                stack.append(iterator.next_value())
                frame.pc += 1
            except StopIteration:
                stack.pop()
                frame.pc = arg
        elif op == "SAFEPOINT":
            frame.pc += 1
            self.safe_points_executed += 1
            if self.safe_point_callback is not None:
                self.safe_point_callback(self)
        elif op == "ASSERT":
            self._require_stack(stack, 2, op)
            message = stack.pop()
            condition = stack.pop()
            if not condition:
                raise AssertionError(message)
            frame.pc += 1
        elif op == "RAISE":
            self._require_stack(stack, 1, op)
            exception = stack.pop()
            if isinstance(exception, type) and issubclass(exception, BaseException):
                raise exception()
            if not isinstance(exception, BaseException):
                raise TypeError("exceptions must derive from BaseException")
            raise exception
        elif op == "SETUP_FINALLY":
            frame.blocks.append(
                {"kind": "finally", "target": arg, "stack_depth": len(stack)}
            )
            frame.pc += 1
        elif op == "POP_BLOCK":
            if not frame.blocks:
                raise RuntimeError("POP_BLOCK without block")
            frame.blocks.pop()
            frame.pc += 1
        elif op == "ENTER_FINALLY_NORMAL":
            frame.finally_reasons.append({"kind": "normal"})
            frame.pc += 1
        elif op == "END_FINALLY":
            if not frame.finally_reasons:
                raise RuntimeError("END_FINALLY without reason")
            reason = frame.finally_reasons.pop()
            frame.pc += 1
            if reason["kind"] == "exception":
                raise reason["exception"]
        else:
            raise ExecutionError(f"unknown opcode: {op!r}")

    def _call_function(
        self, function: FunctionValue, positional: list[Any], kwargs: dict[str, Any]
    ) -> None:
        definition = self.ir["functions"].get(function.function_id)
        if definition is None:
            raise NameError(function.function_id)
        parameters = definition["params"]
        if len(positional) + len(kwargs) != len(parameters):
            raise TypeError(
                f"{definition['name']}() expects {len(parameters)} arguments"
            )
        locals_dict: dict[str, Any] = {}
        for name, value in zip(parameters, positional):
            locals_dict[name] = value
        for name, value in kwargs.items():
            if name not in parameters or name in locals_dict:
                raise TypeError(f"invalid argument {name!r}")
            locals_dict[name] = value
        if len(locals_dict) != len(parameters):
            raise TypeError(f"missing arguments for {definition['name']}()")
        self.frames.append(Frame(function.function_id, 0, locals_dict))

    def _call_host(
        self, callable_value: Any, positional: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        if isinstance(callable_value, BuiltinRef):
            if callable_value.name == "open":
                return self.resources.open_file(*positional, **kwargs)
            function = getattr(builtins, callable_value.name)
            return function(*positional, **kwargs)
        if isinstance(callable_value, ModuleAttrRef):
            if callable_value.module not in ALLOWED_MODULES:
                raise ExecutionError(f"module is not allowed: {callable_value.module}")
            if callable_value.attr not in ALLOWED_MODULE_ATTRS[callable_value.module]:
                raise ExecutionError(
                    f"module function is not allowed: "
                    f"{callable_value.module}.{callable_value.attr}"
                )
            module = importlib.import_module(callable_value.module)
            target = getattr(module, callable_value.attr)
            return target(*positional, **kwargs)
        if isinstance(callable_value, BoundAttrRef):
            receiver = callable_value.receiver
            allowed = set()
            for receiver_type, names in ALLOWED_METHODS.items():
                if isinstance(receiver, receiver_type):
                    allowed = names
                    break
            if callable_value.attr not in allowed:
                raise ExecutionError(
                    f"method is not allowed: "
                    f"{type(receiver).__name__}.{callable_value.attr}"
                )
            target = getattr(receiver, callable_value.attr)
            return target(*positional, **kwargs)
        raise TypeError(f"object is not callable: {type(callable_value).__name__}")

    def _handle_exception(self, exception: BaseException) -> bool:
        while self.frames:
            frame = self.frames[-1]
            if frame.blocks:
                block = frame.blocks.pop()
                if block["kind"] != "finally":
                    raise ExecutionError(f"unknown control block: {block['kind']}")
                del frame.stack[block["stack_depth"] :]
                frame.finally_reasons.append(
                    {"kind": "exception", "exception": exception}
                )
                frame.pc = block["target"]
                return True
            self.frames.pop()
        return False

    def _validate_state(self) -> None:
        if not isinstance(self.frames, list) or not self.frames:
            raise ImageError("continuation has no active frames")
        if not isinstance(self.globals, dict):
            raise ImageError("module globals are invalid")
        for frame in self.frames:
            definition = self.ir["functions"].get(frame.function_id)
            if definition is None:
                raise ImageError(f"unknown frame function: {frame.function_id}")
            if not isinstance(frame.pc, int) or not 0 <= frame.pc < len(
                definition["code"]
            ):
                raise ImageError(f"invalid PC in frame {frame.function_id}")
            if not isinstance(frame.locals, dict) or not isinstance(frame.stack, list):
                raise ImageError(f"invalid frame state: {frame.function_id}")

    @staticmethod
    def _constant(spec: dict[str, Any]) -> Any:
        kind = spec["kind"]
        if kind == "none":
            return None
        if kind == "bool":
            return spec["value"]
        if kind == "int":
            return int(spec["value"])
        if kind == "float":
            return float.fromhex(spec["value"])
        if kind == "str":
            return spec["value"]
        if kind == "bytes":
            return bytes.fromhex(spec["value"])
        raise ExecutionError(f"unknown constant type: {kind}")

    @staticmethod
    def _require_stack(stack: list[Any], count: int, op: str) -> None:
        if len(stack) < count:
            raise ExecutionError(f"operand stack underflow in {op}")

    @classmethod
    def _pop_many(cls, stack: list[Any], count: int) -> list[Any]:
        cls._require_stack(stack, count, "pop")
        if count == 0:
            return []
        values = stack[-count:]
        del stack[-count:]
        return values


VALID_OPS = {
    "ASSERT",
    "BINARY",
    "BUILD_DICT",
    "BUILD_LIST",
    "BUILD_SET",
    "BUILD_SLICE",
    "BUILD_STRING",
    "BUILD_TUPLE",
    "CALL",
    "COMPARE",
    "CONST",
    "END_FINALLY",
    "ENTER_FINALLY_NORMAL",
    "FORMAT_VALUE",
    "FOR_ITER",
    "GET_ITER",
    "IMPORT_MODULE",
    "JUMP",
    "JUMP_IF_FALSE",
    "JUMP_IF_FALSE_OR_POP",
    "JUMP_IF_TRUE_OR_POP",
    "LOAD_ATTR",
    "LOAD_NAME",
    "LOAD_SUBSCR",
    "MAKE_FUNCTION",
    "POP_BLOCK",
    "POP_TOP",
    "RAISE",
    "RETURN",
    "SAFEPOINT",
    "SETUP_FINALLY",
    "STORE_NAME",
    "STORE_SUBSCR_VALUE_FIRST",
    "UNARY",
    "UNPACK",
}
JUMP_OPS = {
    "FOR_ITER",
    "JUMP",
    "JUMP_IF_FALSE",
    "JUMP_IF_FALSE_OR_POP",
    "JUMP_IF_TRUE_OR_POP",
    "SETUP_FINALLY",
}


def validate_ir(ir: dict[str, Any]) -> None:
    if not isinstance(ir, dict) or ir.get("ir_version") != IR_VERSION:
        raise ImageError("unsupported or invalid IR")
    functions = ir.get("functions")
    entry = ir.get("entry_function")
    if not isinstance(functions, dict) or entry not in functions:
        raise ImageError("IR has no valid entry function")
    if set(ir.get("imports", [])) - ALLOWED_MODULES:
        raise ImageError("IR requests a module outside the allowlist")
    instruction_count = 0
    for function_id, definition in functions.items():
        if not isinstance(function_id, str) or not isinstance(definition, dict):
            raise ImageError("invalid function table")
        if definition.get("id") != function_id:
            raise ImageError("function ID mismatch")
        code = definition.get("code")
        params = definition.get("params")
        local_names = definition.get("local_names")
        if (
            not isinstance(code, list)
            or not code
            or not isinstance(params, list)
            or not isinstance(local_names, list)
            or any(not isinstance(name, str) for name in params + local_names)
            or not set(params) <= set(local_names)
        ):
            raise ImageError(f"invalid function definition: {function_id}")
        instruction_count += len(code)
        if instruction_count > 5_000_000:
            raise ImageError("IR instruction limit exceeded")
        for instruction in code:
            if (
                not isinstance(instruction, dict)
                or instruction.get("op") not in VALID_OPS
                or not isinstance(instruction.get("line"), int)
            ):
                raise ImageError(f"invalid instruction in {function_id}")
            if instruction["op"] in JUMP_OPS:
                target = instruction.get("arg")
                if not isinstance(target, int) or not 0 <= target < len(code):
                    raise ImageError(f"invalid jump target in {function_id}")
