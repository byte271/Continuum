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
    EMPTY,
    BoundAttrRef,
    BoundMethodValue,
    ClassValue,
    InstanceValue,
    Cell,
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
# Exception types a program may name. Every one is either raisable by an
# allowlisted operation or a base class of one, so a handler can be written for
# anything this runtime can actually produce. All live in `builtins`, so the
# codec can encode an instance by name and rebuild it on any host.
ALLOWED_EXCEPTIONS = {
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BaseException",
    "Exception",
    "IndexError",
    "KeyError",
    "LookupError",
    "NameError",
    "OSError",
    "OverflowError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "UnboundLocalError",
    "ValueError",
    "ZeroDivisionError",
}
ALLOWED_BUILTINS = {
    *ALLOWED_EXCEPTIONS,
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
    # Closed-over bindings, kept separate from `locals` so a captured name is
    # one shared Cell rather than a per-frame copy.
    cells: dict[str, Cell] = field(default_factory=dict)
    # True for an __init__ frame: the caller already holds the new instance,
    # so this frame's return value is checked and dropped rather than pushed.
    discard_result: bool = False

    def to_state(self) -> dict[str, Any]:
        return {
            "function_id": self.function_id,
            "pc": self.pc,
            "locals": self.locals,
            "stack": self.stack,
            "blocks": self.blocks,
            "finally_reasons": self.finally_reasons,
            "cells": self.cells,
            "discard_result": self.discard_result,
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
            cells=state.get("cells", {}),
            discard_result=state.get("discard_result", False),
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
            # Pushed in order: defaults, keyword defaults, closure cells.
            closure = tuple(self._pop_many(stack, arg.get("closure_count", 0)))
            for item in closure:
                if not isinstance(item, Cell):
                    raise ExecutionError("closure entry is not a cell")
            keyword_defaults = tuple(
                self._pop_many(stack, arg.get("kw_default_count", 0))
            )
            defaults = tuple(self._pop_many(stack, arg["default_count"]))
            stack.append(
                FunctionValue(
                    arg["function_id"], defaults, keyword_defaults, closure
                )
            )
            frame.pc += 1
        elif op == "MAKE_CLASS":
            names = arg["members"]
            values = self._pop_many(stack, len(names))
            stack.append(
                ClassValue(arg["class_id"], arg["name"], dict(zip(names, values)))
            )
            frame.pc += 1
        elif op == "STORE_ATTR_VALUE_FIRST":
            self._require_stack(stack, 2, op)
            target = stack.pop()
            value = stack.pop()
            if not isinstance(target, InstanceValue):
                raise AttributeError(
                    f"cannot set attribute on "
                    f"{self._type_name(target)} object"
                )
            target.attributes[arg] = value
            frame.pc += 1
        elif op == "MAKE_CELL":
            frame.cells[arg] = Cell()
            frame.pc += 1
        elif op == "LOAD_CLOSURE":
            cell = frame.cells.get(arg)
            if cell is None:
                raise ExecutionError(f"no cell for {arg!r}")
            stack.append(cell)
            frame.pc += 1
        elif op == "LOAD_DEREF":
            cell = frame.cells.get(arg)
            if cell is None:
                raise ExecutionError(f"no cell for {arg!r}")
            stack.append(cell.get(arg))
            frame.pc += 1
        elif op == "STORE_DEREF":
            cell = frame.cells.get(arg)
            if cell is None:
                raise ExecutionError(f"no cell for {arg!r}")
            self._require_stack(stack, 1, op)
            cell.set(stack.pop())
            frame.pc += 1
        elif op == "LIST_EXTEND":
            self._require_stack(stack, 2, op)
            addition = stack.pop()
            if not isinstance(addition, (list, tuple, set, frozenset, range)):
                raise TypeError(
                    f"{self._callee_label(stack[-2])}argument after * must be "
                    f"an iterable, not {type(addition).__name__}"
                )
            stack[-1].extend(addition)
            frame.pc += 1
        elif op == "DICT_MERGE":
            self._require_stack(stack, 2, op)
            addition = stack.pop()
            if not isinstance(addition, dict):
                raise TypeError(
                    f"{self._callee_label(stack[-3])}argument after ** must "
                    f"be a mapping, not {type(addition).__name__}"
                )
            target = stack[-1]
            for key, value in addition.items():
                if not isinstance(key, str):
                    raise TypeError("keywords must be strings")
                if key in target:
                    # CPython names the callee here because the merge happens
                    # at the call site. The callee is still on the stack.
                    raise TypeError(
                        f"{self._callee_label(stack[-3])}got multiple values "
                        f"for keyword argument {key!r}"
                    )
                target[key] = value
            frame.pc += 1
        elif op == "CALL_EX":
            self._require_stack(stack, 3, op)
            kwargs = stack.pop()
            positional = stack.pop()
            callable_value = stack.pop()
            if isinstance(callable_value, FunctionValue):
                frame.pc += 1
                self._call_function(callable_value, list(positional), kwargs)
            elif isinstance(callable_value, BoundMethodValue):
                frame.pc += 1
                self._call_function(
                    callable_value.function,
                    [callable_value.instance, *positional],
                    kwargs,
                )
            elif isinstance(callable_value, ClassValue):
                frame.pc += 1
                self._instantiate(
                    callable_value, list(positional), kwargs, stack
                )
            else:
                result = self._call_host(
                    callable_value, list(positional), kwargs
                )
                stack.append(result)
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
            elif isinstance(callable_value, BoundMethodValue):
                frame.pc += 1
                self._call_function(
                    callable_value.function,
                    [callable_value.instance, *positional],
                    kwargs,
                )
            elif isinstance(callable_value, ClassValue):
                frame.pc += 1
                self._instantiate(callable_value, positional, kwargs, stack)
            else:
                result = self._call_host(callable_value, positional, kwargs)
                stack.append(result)
                frame.pc += 1
        elif op == "RETURN":
            self._require_stack(stack, 1, op)
            value = stack.pop()
            finished = self.frames.pop()
            if finished.discard_result:
                if value is not None:
                    raise TypeError(
                        f"__init__() should return None, not "
                        f"{self._type_name(value)!r}"
                    )
            elif self.frames:
                self.frames[-1].stack.append(value)
            elif not self.frames:
                self.result = value
        elif op == "IMPORT_MODULE":
            if arg not in ALLOWED_MODULES:
                raise ImportError(
                    f"module {arg!r} is not in the Continuum allowlist"
                )
            stack.append(ModuleRef(arg))
            frame.pc += 1
        elif op == "LOAD_ATTR":
            self._require_stack(stack, 1, op)
            receiver = stack.pop()
            if isinstance(receiver, ModuleRef):
                stack.append(ModuleAttrRef(receiver.name, arg))
            elif isinstance(receiver, InstanceValue):
                stack.append(self._instance_attribute(receiver, arg))
            elif isinstance(receiver, ClassValue):
                if arg not in receiver.members:
                    raise AttributeError(
                        f"type object {receiver.name!r} has no attribute {arg!r}"
                    )
                stack.append(receiver.members[arg])
            else:
                stack.append(BoundAttrRef(receiver, arg))
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
                    "Continuum cannot checkpoint iteration over "
                    f"{type(iterable).__name__}"
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
        elif op == "SETUP_EXCEPT":
            frame.blocks.append(
                {"kind": "except", "target": arg, "stack_depth": len(stack)}
            )
            frame.pc += 1
        elif op == "DUP_TOP":
            self._require_stack(stack, 1, op)
            stack.append(stack[-1])
            frame.pc += 1
        elif op == "MATCH_EXC":
            # Stack: [..., exception, matcher]. Leaves the exception in place
            # so an unmatched handler can hand it to the next one.
            self._require_stack(stack, 2, op)
            matcher = stack.pop()
            exception = stack.pop()
            stack.append(self._exception_matches(exception, matcher))
            frame.pc += 1
        elif op == "RERAISE":
            self._require_stack(stack, 1, op)
            exception = stack.pop()
            if not isinstance(exception, BaseException):
                raise ExecutionError("RERAISE without a live exception")
            raise exception
        elif op == "DELETE_CELL":
            cell = frame.cells.get(arg)
            if cell is None:
                raise ExecutionError(f"no cell for {arg!r}")
            cell.value = EMPTY
            frame.pc += 1
        elif op == "DELETE_NAME":
            # `except E as name` unbinds `name` when the handler exits, even
            # if the handler never assigned it again.
            frame.locals.pop(arg, None)
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
        self,
        function: FunctionValue,
        positional: list[Any],
        kwargs: dict[str, Any],
        discard_result: bool = False,
    ) -> None:
        definition = self.ir["functions"].get(function.function_id)
        if definition is None:
            raise NameError(function.function_id)
        name = definition["name"]
        parameters = definition["params"]
        posonly_count = definition.get("posonly_count", 0)
        vararg = definition.get("vararg")
        keyword_only = definition.get("kwonly", [])
        kwarg = definition.get("kwarg")
        keyword_default_names = definition.get("kw_default_names", [])
        if len(function.defaults) != definition["default_count"] or len(
            function.kw_defaults
        ) != len(keyword_default_names):
            raise TypeError(f"invalid default state for {name}()")

        locals_dict: dict[str, Any] = {}
        if len(positional) > len(parameters) and vararg is None:
            raise TypeError(
                f"{name}() takes {self._argument_count_phrase(definition)} "
                f"but {len(positional)} "
                f"{'was' if len(positional) == 1 else 'were'} given"
            )
        for parameter, value in zip(parameters, positional):
            locals_dict[parameter] = value
        if vararg is not None:
            locals_dict[vararg] = tuple(positional[len(parameters) :])

        positional_only = set(parameters[:posonly_count])
        bindable = set(parameters[posonly_count:]) | set(keyword_only)
        extra: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in bindable:
                if key in locals_dict:
                    raise TypeError(
                        f"{name}() got multiple values for argument {key!r}"
                    )
                locals_dict[key] = value
            elif kwarg is not None:
                # A positional-only name given by keyword is not an error when
                # the function collects **kwargs; it lands there instead.
                extra[key] = value
            elif key in positional_only:
                raise TypeError(
                    f"{name}() got some positional-only arguments passed as "
                    f"keyword arguments: '{key}'"
                )
            else:
                raise TypeError(
                    f"{name}() got an unexpected keyword argument {key!r}"
                )
        if kwarg is not None:
            locals_dict[kwarg] = extra

        first_default = len(parameters) - definition["default_count"]
        for index, value in enumerate(function.defaults, first_default):
            locals_dict.setdefault(parameters[index], value)
        for key, value in zip(keyword_default_names, function.kw_defaults):
            locals_dict.setdefault(key, value)

        missing = [item for item in parameters if item not in locals_dict]
        if missing:
            raise TypeError(
                f"{name}() missing {len(missing)} required positional "
                f"argument{'' if len(missing) == 1 else 's'}: "
                + self._name_list(missing)
            )
        missing = [item for item in keyword_only if item not in locals_dict]
        if missing:
            raise TypeError(
                f"{name}() missing {len(missing)} required keyword-only "
                f"argument{'' if len(missing) == 1 else 's'}: "
                + self._name_list(missing)
            )
        free = definition.get("freevars", [])
        if len(function.closure) != len(free):
            raise TypeError(f"invalid closure state for {name}()")
        cells: dict[str, Cell] = dict(zip(free, function.closure))
        # A parameter that a nested function captures is moved into its cell,
        # so the callee and every closure it builds read one binding.
        for cellvar in definition.get("cellvars", []):
            cell = Cell()
            if cellvar in locals_dict:
                cell.set(locals_dict.pop(cellvar))
            cells[cellvar] = cell
        self.frames.append(
            Frame(
                function.function_id,
                0,
                locals_dict,
                cells=cells,
                discard_result=discard_result,
            )
        )

    @staticmethod
    def _argument_count_phrase(definition: dict[str, Any]) -> str:
        total = len(definition["params"])
        required = total - definition["default_count"]
        if definition["default_count"]:
            phrase = f"from {required} to {total} positional arguments"
        else:
            phrase = (
                f"{total} positional argument"
                if total == 1
                else f"{total} positional arguments"
            )
        return phrase

    @staticmethod
    def _type_name(value: Any) -> str:
        if isinstance(value, InstanceValue):
            return value.cls.name
        if isinstance(value, ClassValue):
            return "type"
        return type(value).__name__

    def _instance_attribute(self, instance: InstanceValue, name: str) -> Any:
        """Instance dictionary first, then the class, as Python does.

        A function found on the class becomes a bound method value. There is
        no descriptor protocol here, which is why inheritance and properties
        are out of this milestone rather than half-supported.
        """

        if name in instance.attributes:
            return instance.attributes[name]
        member = instance.cls.members.get(name)
        if member is None and name not in instance.cls.members:
            raise AttributeError(
                f"{instance.cls.name!r} object has no attribute {name!r}"
            )
        if isinstance(member, FunctionValue):
            return BoundMethodValue(instance, member)
        return member

    def _instantiate(
        self,
        cls: ClassValue,
        positional: list[Any],
        kwargs: dict[str, Any],
        stack: list[Any],
    ) -> None:
        instance = InstanceValue(cls, {})
        initializer = cls.members.get("__init__")
        if initializer is None:
            if positional or kwargs:
                raise TypeError(
                    f"{cls.name}() takes no arguments"
                )
            stack.append(instance)
            return
        if not isinstance(initializer, FunctionValue):
            raise TypeError(f"{cls.name}.__init__ is not callable")
        # The new instance is pushed first so the frame that __init__ returns
        # into leaves it on the stack as the call's value.
        stack.append(instance)
        self._call_function(
            initializer, [instance, *positional], kwargs, discard_result=True
        )

    def _callee_label(self, callable_value: Any) -> str:
        """Rebuild CPython's `__main__.f() ` call-site prefix.

        A function id is `<parent name>.<name>@<line>`, so the dotted path
        rebuilds the qualname CPython reports, with `<locals>` between levels.
        Only the parent recorded in the id is available, so a function nested
        more than one level deep gets a shorter prefix than CPython's.
        """

        if not isinstance(callable_value, FunctionValue):
            return ""
        if callable_value.function_id not in self.ir["functions"]:
            return ""
        path = callable_value.function_id.rsplit("@", 1)[0].split(".")
        if path and path[0] == "__module__":
            path = path[1:]
        if not path:
            return ""
        return f"__main__.{'.<locals>.'.join(path)}() "

    @staticmethod
    def _name_list(names: list[str]) -> str:
        quoted = [f"'{item}'" for item in names]
        if len(quoted) == 1:
            return quoted[0]
        return ", ".join(quoted[:-1]) + " and " + quoted[-1]

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

    @staticmethod
    def _exception_matches(exception: Any, matcher: Any) -> bool:
        # A handler names its type through the ordinary portable value path, so
        # the matcher arrives as a BuiltinRef rather than a host type object.
        # Resolve it here against the closed exception allowlist; nothing else
        # is accepted, so an image cannot smuggle in an arbitrary type.
        raw = matcher if isinstance(matcher, tuple) else (matcher,)
        candidates = []
        for candidate in raw:
            if isinstance(candidate, BuiltinRef):
                if candidate.name not in ALLOWED_EXCEPTIONS:
                    raise TypeError(
                        "catching classes that do not inherit from "
                        "BaseException is not allowed"
                    )
                candidates.append(getattr(builtins, candidate.name))
                continue
            raise TypeError(
                "catching classes that do not inherit from BaseException "
                "is not allowed"
            )
        return isinstance(exception, tuple(candidates))

    def _handle_exception(self, exception: BaseException) -> bool:
        while self.frames:
            frame = self.frames[-1]
            if frame.blocks:
                block = frame.blocks.pop()
                kind = block["kind"]
                if kind == "finally":
                    del frame.stack[block["stack_depth"] :]
                    frame.finally_reasons.append(
                        {"kind": "exception", "exception": exception}
                    )
                    frame.pc = block["target"]
                    return True
                if kind == "except":
                    # The handler receives the live exception as an ordinary
                    # operand-stack value, so a checkpoint taken anywhere
                    # inside the handler serializes it like any other value.
                    del frame.stack[block["stack_depth"] :]
                    frame.stack.append(exception)
                    frame.pc = block["target"]
                    return True
                raise ExecutionError(f"unknown control block: {kind}")
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
            self._validate_frame_control(frame, definition)

    @staticmethod
    def _validate_frame_control(
        frame: "Frame", definition: dict[str, Any]
    ) -> None:
        # Control blocks decide where a later exception resumes, so a restored
        # image must be checked here rather than when an exception happens to
        # unwind into one.
        if not isinstance(frame.blocks, list) or not isinstance(
            frame.finally_reasons, list
        ):
            raise ImageError(f"invalid control state: {frame.function_id}")
        # A restored frame must hold exactly the cells its function closes
        # over, and each must really be a cell: a wrong shape here would only
        # surface later as a misread binding.
        if not isinstance(frame.cells, dict):
            raise ImageError(f"invalid cell state: {frame.function_id}")
        expected = set(definition.get("cellvars", [])) | set(
            definition.get("freevars", [])
        )
        if set(frame.cells) != expected:
            raise ImageError(
                f"frame {frame.function_id} does not carry its closed-over "
                "bindings"
            )
        for name, cell in frame.cells.items():
            if not isinstance(name, str) or not isinstance(cell, Cell):
                raise ImageError(
                    f"invalid cell binding in {frame.function_id}"
                )
        for block in frame.blocks:
            if not isinstance(block, dict):
                raise ImageError(f"invalid control block: {frame.function_id}")
            if block.get("kind") not in CONTROL_BLOCK_KINDS:
                raise ImageError(
                    f"unknown control block: {block.get('kind')!r}"
                )
            target = block.get("target")
            if not isinstance(target, int) or not 0 <= target < len(
                definition["code"]
            ):
                raise ImageError(
                    f"control block target outside {frame.function_id}"
                )
            depth = block.get("stack_depth")
            if not isinstance(depth, int) or not 0 <= depth <= len(frame.stack):
                raise ImageError(
                    f"control block stack depth is invalid in "
                    f"{frame.function_id}"
                )
        for reason in frame.finally_reasons:
            if not isinstance(reason, dict) or reason.get("kind") not in {
                "normal",
                "exception",
            }:
                raise ImageError(
                    f"unknown finally reason in {frame.function_id}"
                )
            if reason["kind"] == "exception" and not isinstance(
                reason.get("exception"), BaseException
            ):
                raise ImageError(
                    f"finally reason carries no exception in "
                    f"{frame.function_id}"
                )

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
    "CALL_EX",
    "COMPARE",
    "CONST",
    "DELETE_CELL",
    "DELETE_NAME",
    "DICT_MERGE",
    "DUP_TOP",
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
    "LIST_EXTEND",
    "LOAD_CLOSURE",
    "LOAD_DEREF",
    "LOAD_SUBSCR",
    "MAKE_CELL",
    "MAKE_CLASS",
    "MAKE_FUNCTION",
    "MATCH_EXC",
    "POP_BLOCK",
    "POP_TOP",
    "RAISE",
    "RERAISE",
    "RETURN",
    "SAFEPOINT",
    "SETUP_EXCEPT",
    "SETUP_FINALLY",
    "STORE_ATTR_VALUE_FIRST",
    "STORE_DEREF",
    "STORE_NAME",
    "STORE_SUBSCR_VALUE_FIRST",
    "UNARY",
    "UNPACK",
}
CONTROL_BLOCK_KINDS = {"except", "finally"}
JUMP_OPS = {
    "FOR_ITER",
    "JUMP",
    "JUMP_IF_FALSE",
    "JUMP_IF_FALSE_OR_POP",
    "JUMP_IF_TRUE_OR_POP",
    "SETUP_EXCEPT",
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
        default_count = definition.get("default_count")
        local_names = definition.get("local_names")
        if (
            not isinstance(code, list)
            or not code
            or not isinstance(params, list)
            or not isinstance(default_count, int)
            or isinstance(default_count, bool)
            or not 0 <= default_count <= len(params)
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
            if instruction["op"] == "MAKE_FUNCTION":
                argument = instruction.get("arg")
                if (
                    not isinstance(argument, dict)
                    or argument.get("function_id") not in functions
                    or not isinstance(argument.get("default_count"), int)
                    or isinstance(argument.get("default_count"), bool)
                    or argument["default_count"]
                    != functions[argument["function_id"]].get("default_count")
                ):
                    raise ImageError(
                        f"invalid function constructor in {function_id}"
                    )
