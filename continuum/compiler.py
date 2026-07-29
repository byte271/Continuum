from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Any

from . import IR_VERSION
from .errors import CompileError


BIN_OPS = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "truediv",
    ast.FloorDiv: "floordiv",
    ast.Mod: "mod",
    ast.Pow: "pow",
    ast.LShift: "lshift",
    ast.RShift: "rshift",
    ast.BitOr: "or",
    ast.BitXor: "xor",
    ast.BitAnd: "and",
}
UNARY_OPS = {
    ast.UAdd: "pos",
    ast.USub: "neg",
    ast.Not: "not",
    ast.Invert: "invert",
}
COMPARE_OPS = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Gt: "gt",
    ast.GtE: "ge",
    ast.Is: "is",
    ast.IsNot: "is_not",
    ast.In: "in",
    ast.NotIn: "not_in",
}


@dataclass
class LoopContext:
    continue_target: int
    breaks: list[int]
    break_stack_cleanup: int


class LocalNameCollector(ast.NodeVisitor):
    """Collect names bound in one Python scope without entering child scopes."""

    def __init__(self, parameters: list[str]):
        self.names = set(parameters)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)


def collect_local_names(body: list[ast.stmt], parameters: list[str]) -> set[str]:
    collector = LocalNameCollector(parameters)
    for statement in body:
        collector.visit(statement)
    return collector.names


class FunctionCompiler:
    def __init__(
        self,
        owner: "ProgramCompiler",
        function_id: str,
        name: str,
        local_names: set[str],
        enclosing_locals: tuple[set[str], ...] = (),
    ):
        self.owner = owner
        self.function_id = function_id
        self.name = name
        self.local_names = local_names
        self.enclosing_locals = enclosing_locals
        self.code: list[dict[str, Any]] = []
        self.loops: list[LoopContext] = []

    def emit(self, op: str, arg: Any = None, line: int = 0) -> int:
        instruction = {"op": op, "line": line}
        if arg is not None:
            instruction["arg"] = arg
        self.code.append(instruction)
        return len(self.code) - 1

    def patch(self, index: int, target: int) -> None:
        self.code[index]["arg"] = target

    def safe(self, node: ast.AST) -> None:
        self.emit("SAFEPOINT", line=getattr(node, "lineno", 0))

    def statements(self, body: list[ast.stmt]) -> None:
        for statement in body:
            self.statement(statement)

    def statement(self, node: ast.stmt) -> None:
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                self.unsupported(node, "chained assignment")
            self.assignment(node.targets[0], node.value, line)
            self.safe(node)
        elif isinstance(node, ast.AnnAssign):
            self.unsupported(node, "annotated assignment")
        elif isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                self.unsupported(node, "augmented assignment to non-name")
            self.emit("LOAD_NAME", node.target.id, line)
            self.expression(node.value)
            self.emit("BINARY", self.lookup(BIN_OPS, node.op, node), line)
            self.emit("STORE_NAME", node.target.id, line)
            self.safe(node)
        elif isinstance(node, ast.Expr):
            self.expression(node.value)
            self.emit("POP_TOP", line=line)
            self.safe(node)
        elif isinstance(node, ast.If):
            self.expression(node.test)
            false_jump = self.emit("JUMP_IF_FALSE", -1, line)
            self.statements(node.body)
            if node.orelse:
                end_jump = self.emit("JUMP", -1, line)
                self.patch(false_jump, len(self.code))
                self.statements(node.orelse)
                self.patch(end_jump, len(self.code))
            else:
                self.patch(false_jump, len(self.code))
        elif isinstance(node, ast.While):
            start = len(self.code)
            self.expression(node.test)
            exit_jump = self.emit("JUMP_IF_FALSE", -1, line)
            context = LoopContext(start, [], 0)
            self.loops.append(context)
            self.statements(node.body)
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", start, line)
            exhausted = len(self.code)
            self.patch(exit_jump, exhausted)
            self.statements(node.orelse)
            end = len(self.code)
            for jump in context.breaks:
                self.patch(jump, end)
            self.loops.pop()
        elif isinstance(node, ast.For):
            self.expression(node.iter)
            self.emit("GET_ITER", line=line)
            start = len(self.code)
            exit_jump = self.emit("FOR_ITER", -1, line)
            self.store_target(node.target, line)
            self.emit("SAFEPOINT", line=line)
            context = LoopContext(start, [], 1)
            self.loops.append(context)
            self.statements(node.body)
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", start, line)
            exhausted = len(self.code)
            self.patch(exit_jump, exhausted)
            self.statements(node.orelse)
            end = len(self.code)
            for jump in context.breaks:
                self.patch(jump, end)
            self.loops.pop()
        elif isinstance(node, ast.Break):
            if not self.loops:
                self.unsupported(node, "break outside loop")
            for _ in range(self.loops[-1].break_stack_cleanup):
                self.emit("POP_TOP", line=line)
            jump = self.emit("JUMP", -1, line)
            self.loops[-1].breaks.append(jump)
        elif isinstance(node, ast.Continue):
            if not self.loops:
                self.unsupported(node, "continue outside loop")
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", self.loops[-1].continue_target, line)
        elif isinstance(node, ast.Return):
            if self.function_id == "__module__":
                self.unsupported(node, "return at module scope")
            if node.value is None:
                self.emit("CONST", {"kind": "none"}, line)
            else:
                self.expression(node.value)
            self.emit("RETURN", line=line)
        elif isinstance(node, ast.FunctionDef):
            function_id = self.owner.compile_function(node, self)
            for default in node.args.defaults:
                self.expression(default)
            self.emit(
                "MAKE_FUNCTION",
                {
                    "function_id": function_id,
                    "default_count": len(node.args.defaults),
                },
                line,
            )
            self.emit("STORE_NAME", node.name, line)
            self.safe(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "." in alias.name and alias.asname is None:
                    self.unsupported(node, "dotted import without 'as'")
                self.owner.imports.add(alias.name)
                self.emit("IMPORT_MODULE", alias.name, line)
                self.emit("STORE_NAME", alias.asname or alias.name, line)
            self.safe(node)
        elif isinstance(node, ast.ImportFrom):
            self.unsupported(node, "from ... import")
        elif isinstance(node, ast.Assert):
            self.expression(node.test)
            if node.msg is None:
                self.emit("CONST", {"kind": "str", "value": "assertion failed"}, line)
            else:
                self.expression(node.msg)
            self.emit("ASSERT", line=line)
            self.safe(node)
        elif isinstance(node, ast.Raise):
            if node.exc is None or node.cause is not None:
                self.unsupported(node, "bare raise or raise ... from")
            self.expression(node.exc)
            self.emit("RAISE", line=line)
        elif isinstance(node, ast.Try):
            self.compile_try_finally(node)
        elif isinstance(node, ast.Pass):
            self.safe(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            self.unsupported(node, "global/nonlocal declaration")
        else:
            self.unsupported(node)

    def compile_try_finally(self, node: ast.Try) -> None:
        if node.handlers or node.orelse or not node.finalbody:
            self.unsupported(node, "only try/finally is supported")
        for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(child, (ast.Return, ast.Break, ast.Continue)):
                self.unsupported(node, "control transfer out of try")
        setup = self.emit("SETUP_FINALLY", -1, node.lineno)
        self.statements(node.body)
        self.emit("POP_BLOCK", line=node.lineno)
        self.emit("ENTER_FINALLY_NORMAL", line=node.lineno)
        jump = self.emit("JUMP", -1, node.lineno)
        handler = len(self.code)
        self.patch(setup, handler)
        self.patch(jump, handler)
        self.emit("SAFEPOINT", line=node.lineno)
        self.statements(node.finalbody)
        self.emit("END_FINALLY", line=node.lineno)
        self.safe(node)

    def assignment(self, target: ast.expr, value: ast.expr, line: int) -> None:
        if isinstance(target, ast.Name):
            self.expression(value)
            self.emit("STORE_NAME", target.id, line)
        elif isinstance(target, ast.Subscript):
            # Python evaluates the right-hand side before every target in a
            # normal assignment. Keep that ordering observable.
            self.expression(value)
            self.expression(target.value)
            self.expression(target.slice)
            self.emit("STORE_SUBSCR_VALUE_FIRST", line=line)
        elif isinstance(target, ast.Attribute):
            self.unsupported(
                target,
                "attribute assignment is not yet state-preserving",
            )
        else:
            self.unsupported(target, "assignment target")

    def store_target(self, target: ast.expr, line: int) -> None:
        if isinstance(target, ast.Name):
            self.emit("STORE_NAME", target.id, line)
        elif isinstance(target, (ast.Tuple, ast.List)):
            self.emit("UNPACK", len(target.elts), line)
            for item in target.elts:
                self.store_target(item, line)
        else:
            self.unsupported(target, "loop target")

    def expression(self, node: ast.expr) -> None:
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant):
            self.emit("CONST", self.constant(node.value, node), line)
        elif isinstance(node, ast.Name):
            if (
                node.id not in self.local_names
                and any(node.id in scope for scope in self.enclosing_locals)
            ):
                self.unsupported(
                    node,
                    f"closure capture of {node.id!r}",
                )
            self.emit("LOAD_NAME", node.id, line)
        elif isinstance(node, ast.List):
            for item in node.elts:
                self.expression(item)
            self.emit("BUILD_LIST", len(node.elts), line)
        elif isinstance(node, ast.Tuple):
            for item in node.elts:
                self.expression(item)
            self.emit("BUILD_TUPLE", len(node.elts), line)
        elif isinstance(node, ast.Set):
            for item in node.elts:
                self.expression(item)
            self.emit("BUILD_SET", len(node.elts), line)
        elif isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                self.unsupported(node, "dictionary unpacking")
            for key, value in zip(node.keys, node.values):
                self.expression(key)
                self.expression(value)
            self.emit("BUILD_DICT", len(node.keys), line)
        elif isinstance(node, ast.BinOp):
            self.expression(node.left)
            self.expression(node.right)
            self.emit("BINARY", self.lookup(BIN_OPS, node.op, node), line)
        elif isinstance(node, ast.UnaryOp):
            self.expression(node.operand)
            self.emit("UNARY", self.lookup(UNARY_OPS, node.op, node), line)
        elif isinstance(node, ast.BoolOp):
            self.compile_bool(node)
        elif isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                self.unsupported(node, "chained comparison")
            self.expression(node.left)
            self.expression(node.comparators[0])
            self.emit("COMPARE", self.lookup(COMPARE_OPS, node.ops[0], node), line)
        elif isinstance(node, ast.Call):
            if any(isinstance(arg, ast.Starred) for arg in node.args):
                self.unsupported(node, "starred call arguments")
            if any(keyword.arg is None for keyword in node.keywords):
                self.unsupported(node, "double-star call arguments")
            self.expression(node.func)
            for arg in node.args:
                self.expression(arg)
            for keyword in node.keywords:
                self.expression(keyword.value)
            self.emit(
                "CALL",
                {
                    "positional": len(node.args),
                    "keywords": [item.arg for item in node.keywords],
                },
                line,
            )
        elif isinstance(node, ast.Attribute):
            self.expression(node.value)
            self.emit("LOAD_ATTR", node.attr, line)
        elif isinstance(node, ast.Subscript):
            self.expression(node.value)
            self.expression(node.slice)
            self.emit("LOAD_SUBSCR", line=line)
        elif isinstance(node, ast.IfExp):
            self.expression(node.test)
            false_jump = self.emit("JUMP_IF_FALSE", -1, line)
            self.expression(node.body)
            end_jump = self.emit("JUMP", -1, line)
            self.patch(false_jump, len(self.code))
            self.expression(node.orelse)
            self.patch(end_jump, len(self.code))
        elif isinstance(node, ast.Slice):
            for part in (node.lower, node.upper, node.step):
                if part is None:
                    self.emit("CONST", {"kind": "none"}, line)
                else:
                    self.expression(part)
            self.emit("BUILD_SLICE", 3, line)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    self.emit("CONST", self.constant(value.value, value), line)
                elif isinstance(value, ast.FormattedValue):
                    if value.conversion != -1:
                        self.unsupported(value, "f-string conversion")
                    self.expression(value.value)
                    if value.format_spec is not None:
                        self.expression(value.format_spec)
                    else:
                        self.emit("CONST", {"kind": "str", "value": ""}, line)
                    self.emit("FORMAT_VALUE", line=line)
                else:
                    self.unsupported(value, "f-string component")
            self.emit("BUILD_STRING", len(node.values), line)
        else:
            self.unsupported(node, "expression")

    def compile_bool(self, node: ast.BoolOp) -> None:
        self.expression(node.values[0])
        jumps = []
        op = "JUMP_IF_FALSE_OR_POP" if isinstance(node.op, ast.And) else "JUMP_IF_TRUE_OR_POP"
        for value in node.values[1:]:
            jumps.append(self.emit(op, -1, node.lineno))
            self.expression(value)
        end = len(self.code)
        for jump in jumps:
            self.patch(jump, end)

    def constant(self, value: Any, node: ast.AST) -> dict[str, Any]:
        if value is None:
            return {"kind": "none"}
        if isinstance(value, bool):
            return {"kind": "bool", "value": value}
        if isinstance(value, int):
            return {"kind": "int", "value": str(value)}
        if isinstance(value, float):
            return {"kind": "float", "value": value.hex()}
        if isinstance(value, str):
            return {"kind": "str", "value": value}
        if isinstance(value, bytes):
            return {"kind": "bytes", "value": value.hex()}
        self.unsupported(node, f"constant {type(value).__name__}")
        raise AssertionError

    def lookup(self, table: dict[type, str], value: Any, node: ast.AST) -> str:
        result = table.get(type(value))
        if result is None:
            self.unsupported(node, type(value).__name__)
        return result

    def unsupported(self, node: ast.AST, detail: str | None = None) -> None:
        name = type(node).__name__
        suffix = f" ({detail})" if detail else ""
        raise CompileError(
            f"{self.owner.source_name}:{getattr(node, 'lineno', 0)}: "
            f"unsupported syntax {name}{suffix}"
        )


class ProgramCompiler:
    def __init__(self, source: str, source_name: str):
        self.source = source
        self.source_name = source_name
        self.functions: dict[str, dict[str, Any]] = {}
        self.imports: set[str] = set()

    def compile(self) -> dict[str, Any]:
        try:
            tree = ast.parse(self.source, filename=self.source_name)
        except SyntaxError as exc:
            raise CompileError(str(exc)) from exc
        module_names = collect_local_names(tree.body, [])
        module = FunctionCompiler(
            self,
            "__module__",
            "__module__",
            module_names,
        )
        module.statements(tree.body)
        module.emit("CONST", {"kind": "none"}, line=len(self.source.splitlines()) or 1)
        module.emit("RETURN", line=len(self.source.splitlines()) or 1)
        self.functions["__module__"] = {
            "id": "__module__",
            "name": "__module__",
            "params": [],
            "default_count": 0,
            "local_names": sorted(module.local_names),
            "code": module.code,
        }
        return {
            "ir_version": IR_VERSION,
            "source_name": self.source_name,
            "source_sha256": hashlib.sha256(self.source.encode("utf-8")).hexdigest(),
            "entry_function": "__module__",
            "imports": sorted(self.imports),
            "functions": self.functions,
        }

    def compile_function(
        self, node: ast.FunctionDef, parent: FunctionCompiler
    ) -> str:
        if node.decorator_list:
            raise CompileError(
                f"{self.source_name}:{node.lineno}: decorators are unsupported"
            )
        if (
            node.args.posonlyargs
            or node.args.kwonlyargs
            or node.args.vararg
            or node.args.kwarg
        ):
            raise CompileError(
                f"{self.source_name}:{node.lineno}: only positional parameters are supported"
            )
        parameters = [arg.arg for arg in node.args.args]
        local_names = collect_local_names(node.body, parameters)
        function_id = f"{parent.name}.{node.name}@{node.lineno}"
        enclosing_locals = parent.enclosing_locals
        if parent.function_id != "__module__":
            enclosing_locals = (*enclosing_locals, parent.local_names)
        compiler = FunctionCompiler(
            self,
            function_id,
            node.name,
            local_names,
            enclosing_locals,
        )
        compiler.statements(node.body)
        compiler.emit("CONST", {"kind": "none"}, node.end_lineno or node.lineno)
        compiler.emit("RETURN", line=node.end_lineno or node.lineno)
        self.functions[function_id] = {
            "id": function_id,
            "name": node.name,
            "params": parameters,
            "default_count": len(node.args.defaults),
            "local_names": sorted(local_names),
            "code": compiler.code,
        }
        return function_id


def compile_source(source: str, source_name: str) -> dict[str, Any]:
    return ProgramCompiler(source, source_name).compile()
