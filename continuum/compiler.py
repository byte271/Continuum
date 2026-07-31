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


def statement_shape(node: ast.stmt) -> str:
    """A structural digest of one statement, independent of where it sits.

    Line numbers are excluded, so moving code does not change identity. For a
    compound statement only the *header* is digested -- a `while` is described
    by its test, a `for` by its target and iterable, a `try` by its handler
    matchers -- so editing a loop body does not change the identity of the loop
    that body belongs to. That separation is what lets an active loop keep its
    identity while the code inside it changes.
    """

    if isinstance(node, ast.While):
        parts = ["While", ast.dump(node.test, annotate_fields=False)]
    elif isinstance(node, ast.For):
        parts = [
            "For",
            ast.dump(node.target, annotate_fields=False),
            ast.dump(node.iter, annotate_fields=False),
        ]
    elif isinstance(node, ast.If):
        parts = ["If", ast.dump(node.test, annotate_fields=False)]
    elif isinstance(node, ast.Try):
        parts = ["Try"]
        for handler in node.handlers:
            parts.append(
                "except:"
                + (
                    "*"
                    if handler.type is None
                    else ast.dump(handler.type, annotate_fields=False)
                )
                + f" as {handler.name or ''}"
            )
        parts.append(f"finally:{bool(node.finalbody)}")
        parts.append(f"else:{bool(node.orelse)}")
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # A nested definition is identified by its signature, not its body, so
        # changing what an inactive nested function does leaves the statement
        # that defines it in place.
        parts = ["Def", node.name, ast.dump(node.args, annotate_fields=False)]
    elif isinstance(node, ast.ClassDef):
        parts = ["Class", node.name]
    else:
        parts = [type(node).__name__, ast.dump(node, annotate_fields=False)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def collect_local_names(body: list[ast.stmt], parameters: list[str]) -> set[str]:
    collector = LocalNameCollector(parameters)
    for statement in body:
        collector.visit(statement)
    return collector.names


class ScopeUseCollector(ast.NodeVisitor):
    """Names read or declared `nonlocal` in one scope, ignoring nested ones."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.nonlocals: set[str] = set()
        self.functions: list[ast.FunctionDef] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Defaults and decorators are evaluated in the enclosing scope.
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        self.functions.append(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)


class Scope:
    """One lexical scope, used to decide which bindings need cells."""

    def __init__(
        self,
        node: ast.AST | None,
        parent: "Scope | None",
        is_function: bool,
        bound: set[str],
    ) -> None:
        self.node = node
        self.parent = parent
        self.is_function = is_function
        self.bound = bound
        self.used: set[str] = set()
        self.nonlocals: set[str] = set()
        self.children: list[Scope] = []
        self.cellvars: set[str] = set()
        self.freevars: set[str] = set()

    def binds_in_function_ancestor(self, name: str) -> bool:
        ancestor = self.parent
        while ancestor is not None and ancestor.is_function:
            if name in ancestor.bound:
                return True
            ancestor = ancestor.parent
        return False


def build_scope(
    body: list[ast.stmt],
    parameters: list[str],
    node: ast.AST | None,
    parent: Scope | None,
    is_function: bool,
) -> Scope:
    scope = Scope(node, parent, is_function, collect_local_names(body, parameters))
    collector = ScopeUseCollector()
    for statement in body:
        collector.visit(statement)
    scope.used = collector.used
    scope.nonlocals = collector.nonlocals
    for child_node in collector.functions:
        arguments = child_node.args
        child_parameters = [
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        ]
        if arguments.vararg:
            child_parameters.append(arguments.vararg.arg)
        if arguments.kwarg:
            child_parameters.append(arguments.kwarg.arg)
        scope.children.append(
            build_scope(
                child_node.body, child_parameters, child_node, scope, True
            )
        )
    return scope


def resolve_scope(scope: Scope) -> None:
    """Mark captured bindings, bottom up.

    A name a nested scope needs and this scope binds becomes a cell here. A
    name neither scope binds keeps travelling outward until a function scope
    binds it; if none does, it is an ordinary global.
    """

    for child in scope.children:
        resolve_scope(child)
    needed = set(scope.used) | set(scope.nonlocals)
    for child in scope.children:
        needed |= child.freevars
        scope.cellvars.update(child.freevars & scope.bound)
    if scope.is_function:
        for name in needed:
            if name not in scope.bound and scope.binds_in_function_ancestor(name):
                scope.freevars.add(name)
    # `nonlocal x` binds x in this scope through the enclosing cell, so it is
    # free here rather than local.
    scope.bound -= scope.nonlocals
    scope.freevars |= scope.nonlocals & {
        name for name in scope.nonlocals if scope.binds_in_function_ancestor(name)
    }


class FunctionCompiler:
    def __init__(
        self,
        owner: "ProgramCompiler",
        function_id: str,
        name: str,
        local_names: set[str],
        enclosing_locals: tuple[set[str], ...] = (),
        scope: "Scope | None" = None,
    ):
        self.owner = owner
        self.function_id = function_id
        self.name = name
        self.local_names = local_names
        self.enclosing_locals = enclosing_locals
        self.scope = scope
        self.cellvars = set(scope.cellvars) if scope else set()
        self.freevars = sorted(scope.freevars) if scope else []
        self.code: list[dict[str, Any]] = []
        # Semantic site per emitted instruction, parallel to `code`. This is a
        # side table: it never enters the IR, so annotating costs no format
        # change and an image written before this existed can still be
        # annotated by recompiling its stored source. See continuum/semantics.py.
        self.sites: list[tuple[Any, ...]] = []
        self.region_path: list[tuple[str, str, int, str]] = []
        self.statement_key: tuple[str, str, int] | None = None
        self.loops: list[LoopContext] = []
        # Enclosing protected regions in this function. `protect_depth` counts
        # every active control block; `finally_depth` counts only those whose
        # cleanup must still run before control leaves.
        self.protect_depth = 0
        self.finally_depth = 0

    def is_cell(self, name: str) -> bool:
        return name in self.cellvars or name in self.freevars

    def load_name(self, name: str, line: int) -> None:
        self.emit("LOAD_DEREF" if self.is_cell(name) else "LOAD_NAME", name, line)

    def store_name(self, name: str, line: int) -> None:
        self.emit(
            "STORE_DEREF" if self.is_cell(name) else "STORE_NAME", name, line
        )

    def emit(self, op: str, arg: Any = None, line: int = 0) -> int:
        instruction = {"op": op, "line": line}
        if arg is not None:
            instruction["arg"] = arg
        self.code.append(instruction)
        self.sites.append((tuple(self.region_path), self.statement_key))
        return len(self.code) - 1

    def patch(self, index: int, target: int) -> None:
        self.code[index]["arg"] = target

    def safe(self, node: ast.AST) -> None:
        self.emit("SAFEPOINT", line=getattr(node, "lineno", 0))

    def statements(self, body: list[ast.stmt]) -> None:
        # Key each statement by its own shape and by how many statements of
        # that same shape precede it in this body, rather than by its child
        # index. Inserting a statement of any *different* shape therefore does
        # not perturb the identity of the statements around it, which is what
        # makes "insert code after the resume point" a mappable edit. Two
        # identically shaped siblings do shift each other's occurrence index,
        # and that ambiguity is reported rather than resolved by guessing.
        occurrences: dict[str, int] = {}
        previous = self.statement_key
        for statement in body:
            shape = statement_shape(statement)
            index = occurrences.get(shape, 0)
            occurrences[shape] = index + 1
            self.statement_key = (type(statement).__name__, shape, index)
            self.statement(statement)
        self.statement_key = previous

    def enter_region(self, part: str) -> None:
        key = self.statement_key or ("", "", 0)
        self.region_path.append((key[0], key[1], key[2], part))

    def exit_region(self) -> None:
        self.region_path.pop()

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
            self.load_name(node.target.id, line)
            self.expression(node.value)
            self.emit("BINARY", self.lookup(BIN_OPS, node.op, node), line)
            self.store_name(node.target.id, line)
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
                self.enter_region("orelse")
                self.statements(node.orelse)
                self.exit_region()
                self.patch(end_jump, len(self.code))
            else:
                self.patch(false_jump, len(self.code))
        elif isinstance(node, ast.While):
            start = len(self.code)
            self.expression(node.test)
            exit_jump = self.emit("JUMP_IF_FALSE", -1, line)
            context = LoopContext(start, [], 0)
            self.loops.append(context)
            self.enter_region("body")
            self.statements(node.body)
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", start, line)
            self.exit_region()
            exhausted = len(self.code)
            self.patch(exit_jump, exhausted)
            self.enter_region("orelse")
            self.statements(node.orelse)
            self.exit_region()
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
            self.enter_region("body")
            self.statements(node.body)
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", start, line)
            self.exit_region()
            exhausted = len(self.code)
            self.patch(exit_jump, exhausted)
            self.enter_region("orelse")
            self.statements(node.orelse)
            self.exit_region()
            end = len(self.code)
            for jump in context.breaks:
                self.patch(jump, end)
            self.loops.pop()
        elif isinstance(node, ast.Break):
            if not self.loops:
                self.unsupported(node, "break outside loop")
            if self.protect_depth:
                # Jumping inside the frame would leave the control block on
                # the frame, so the next exception would unwind to a region
                # the program already left.
                self.unsupported(node, "break out of try")
            for _ in range(self.loops[-1].break_stack_cleanup):
                self.emit("POP_TOP", line=line)
            jump = self.emit("JUMP", -1, line)
            self.loops[-1].breaks.append(jump)
        elif isinstance(node, ast.Continue):
            if not self.loops:
                self.unsupported(node, "continue outside loop")
            if self.protect_depth:
                self.unsupported(node, "continue out of try")
            self.emit("SAFEPOINT", line=line)
            self.emit("JUMP", self.loops[-1].continue_target, line)
        elif isinstance(node, ast.Return):
            if self.function_id == "__module__":
                self.unsupported(node, "return at module scope")
            if self.finally_depth:
                # RETURN discards the frame, which would skip a finally body
                # that is still owed. Returning out of try/except alone is
                # safe: the frame's control blocks die with the frame.
                self.unsupported(node, "return out of try/finally")
            if node.value is None:
                self.emit("CONST", {"kind": "none"}, line)
            else:
                self.expression(node.value)
            self.emit("RETURN", line=line)
        elif isinstance(node, ast.FunctionDef):
            function_id, captured = self.owner.compile_function(node, self)
            for default in node.args.defaults:
                self.expression(default)
            keyword_defaults = [
                default
                for default in node.args.kw_defaults
                if default is not None
            ]
            for default in keyword_defaults:
                self.expression(default)
            for name in captured:
                # The same cell object the enclosing frame holds, so the two
                # scopes share one binding rather than a copy.
                self.emit("LOAD_CLOSURE", name, line)
            self.emit(
                "MAKE_FUNCTION",
                {
                    "function_id": function_id,
                    "default_count": len(node.args.defaults),
                    "kw_default_count": len(keyword_defaults),
                    "closure_count": len(captured),
                },
                line,
            )
            self.store_name(node.name, line)
            self.safe(node)
        elif isinstance(node, ast.ClassDef):
            self.compile_class(node, line)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "." in alias.name and alias.asname is None:
                    self.unsupported(node, "dotted import without 'as'")
                self.owner.imports.add(alias.name)
                self.emit("IMPORT_MODULE", alias.name, line)
                self.store_name(alias.asname or alias.name, line)
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
            self.compile_try(node)
        elif isinstance(node, ast.Pass):
            self.safe(node)
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                if name not in self.freevars:
                    self.unsupported(node, f"no binding for nonlocal {name!r}")
            self.safe(node)
        elif isinstance(node, ast.Global):
            self.unsupported(node, "global declaration")
        else:
            self.unsupported(node)

    def compile_try(self, node: ast.Try) -> None:
        if not node.handlers and not node.finalbody:
            self.unsupported(node, "try without except or finally")
        if node.orelse and not node.handlers:
            self.unsupported(node, "try/else without except")
        if node.finalbody and node.handlers:
            # try/except/finally is exactly a try/finally wrapping a
            # try/except, and reusing that nesting keeps one implementation of
            # each construct.
            inner = ast.Try(
                body=node.body,
                handlers=node.handlers,
                orelse=node.orelse,
                finalbody=[],
            )
            ast.copy_location(inner, node)
            self.compile_try_finally_region([inner], node)
            return
        if node.finalbody:
            self.compile_try_finally_region(node.body, node)
            return
        self.compile_try_except(node)

    def compile_try_finally_region(
        self, body: list[ast.stmt], node: ast.Try
    ) -> None:
        setup = self.emit("SETUP_FINALLY", -1, node.lineno)
        self.protect_depth += 1
        self.finally_depth += 1
        self.enter_region("try")
        self.statements(body)
        self.exit_region()
        self.finally_depth -= 1
        self.protect_depth -= 1
        self.emit("POP_BLOCK", line=node.lineno)
        self.emit("ENTER_FINALLY_NORMAL", line=node.lineno)
        jump = self.emit("JUMP", -1, node.lineno)
        handler = len(self.code)
        self.patch(setup, handler)
        self.patch(jump, handler)
        self.emit("SAFEPOINT", line=node.lineno)
        self.enter_region("finally")
        self.statements(node.finalbody)
        self.exit_region()
        self.emit("END_FINALLY", line=node.lineno)
        self.safe(node)

    def compile_try_except(self, node: ast.Try) -> None:
        setup = self.emit("SETUP_EXCEPT", -1, node.lineno)
        self.protect_depth += 1
        self.enter_region("try")
        self.statements(node.body)
        self.exit_region()
        self.protect_depth -= 1
        self.emit("POP_BLOCK", line=node.lineno)
        self.enter_region("orelse")
        self.statements(node.orelse)
        self.exit_region()
        normal_jump = self.emit("JUMP", -1, node.lineno)

        # Handler dispatch. On entry the live exception is the top of stack.
        self.patch(setup, len(self.code))
        self.emit("SAFEPOINT", line=node.lineno)
        end_jumps: list[int] = []
        saw_bare = False
        for handler_index, handler in enumerate(node.handlers):
            line = handler.lineno
            if saw_bare:
                self.unsupported(handler, "except clause after a bare except")
            if handler.type is None:
                saw_bare = True
                next_handler = None
            else:
                self.emit("DUP_TOP", line=line)
                self.expression(handler.type)
                self.emit("MATCH_EXC", line=line)
                next_handler = self.emit("JUMP_IF_FALSE", -1, line)
            if handler.name is None:
                self.emit("POP_TOP", line=line)
            else:
                self.store_name(handler.name, line)
                self.local_names.add(handler.name)
            self.emit("SAFEPOINT", line=line)
            self.enter_region(f"handler:{handler_index}")
            self.statements(handler.body)
            self.exit_region()
            if handler.name is not None:
                # CPython unbinds the handler name when the block exits.
                self.emit(
                    "DELETE_CELL" if self.is_cell(handler.name) else "DELETE_NAME",
                    handler.name,
                    line,
                )
            end_jumps.append(self.emit("JUMP", -1, line))
            if next_handler is not None:
                self.patch(next_handler, len(self.code))
        if not saw_bare:
            # Nothing matched: the exception is still on the stack.
            self.emit("RERAISE", line=node.lineno)
        end = len(self.code)
        self.patch(normal_jump, end)
        for jump in end_jumps:
            self.patch(jump, end)
        self.safe(node)

    def assignment(self, target: ast.expr, value: ast.expr, line: int) -> None:
        if isinstance(target, ast.Name):
            self.expression(value)
            self.store_name(target.id, line)
        elif isinstance(target, ast.Subscript):
            # Python evaluates the right-hand side before every target in a
            # normal assignment. Keep that ordering observable.
            self.expression(value)
            self.expression(target.value)
            self.expression(target.slice)
            self.emit("STORE_SUBSCR_VALUE_FIRST", line=line)
        elif isinstance(target, ast.Attribute):
            # Python evaluates the right-hand side first here too.
            self.expression(value)
            self.expression(target.value)
            self.emit("STORE_ATTR_VALUE_FIRST", target.attr, line)
        else:
            self.unsupported(target, "assignment target")

    def store_target(self, target: ast.expr, line: int) -> None:
        if isinstance(target, ast.Name):
            self.store_name(target.id, line)
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
            self.load_name(node.id, line)
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
            if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
                keyword.arg is None for keyword in node.keywords
            ):
                self.compile_unpacking_call(node, line)
                return
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

    def compile_class(self, node: ast.ClassDef, line: int) -> None:
        """Build a VM-owned class from a restricted body.

        Only method definitions and simple `name = expression` members are
        accepted. Anything else, including a base class or a metaclass, is
        rejected rather than silently reinterpreted.
        """

        if node.bases or node.keywords:
            self.unsupported(node, "base classes and metaclasses")
        if node.decorator_list:
            self.unsupported(node, "class decorators")

        members: list[str] = []
        for statement in node.body:
            if isinstance(statement, ast.FunctionDef):
                function_id, captured = self.owner.compile_function(
                    statement, self
                )
                for default in statement.args.defaults:
                    self.expression(default)
                keyword_defaults = [
                    default
                    for default in statement.args.kw_defaults
                    if default is not None
                ]
                for default in keyword_defaults:
                    self.expression(default)
                for name in captured:
                    self.emit("LOAD_CLOSURE", name, statement.lineno)
                self.emit(
                    "MAKE_FUNCTION",
                    {
                        "function_id": function_id,
                        "default_count": len(statement.args.defaults),
                        "kw_default_count": len(keyword_defaults),
                        "closure_count": len(captured),
                    },
                    statement.lineno,
                )
                members.append(statement.name)
            elif isinstance(statement, ast.Assign) and len(
                statement.targets
            ) == 1 and isinstance(statement.targets[0], ast.Name):
                self.expression(statement.value)
                members.append(statement.targets[0].id)
            elif isinstance(statement, ast.Pass):
                continue
            elif isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Constant
            ):
                continue  # docstring
            else:
                self.unsupported(statement, "class body statement")

        if len(set(members)) != len(members):
            self.unsupported(node, "duplicate class member")
        self.emit(
            "MAKE_CLASS",
            {
                "class_id": f"{self.name}.{node.name}@{node.lineno}",
                "name": node.name,
                "members": members,
            },
            line,
        )
        self.store_name(node.name, line)
        self.safe(node)

    def compile_unpacking_call(self, node: ast.Call, line: int) -> None:
        """Compile a call containing `*args` or `**kwargs`.

        Arguments are gathered into one list and one dict so the callee sees a
        single portable pair. Buffered plain arguments are flushed before each
        unpacking so evaluation stays strictly left to right.
        """

        self.expression(node.func)

        self.emit("BUILD_LIST", 0, line)
        buffered = 0
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                if buffered:
                    self.emit("BUILD_LIST", buffered, line)
                    self.emit("LIST_EXTEND", line=line)
                    buffered = 0
                self.expression(argument.value)
                self.emit("LIST_EXTEND", line=line)
            else:
                self.expression(argument)
                buffered += 1
        if buffered:
            self.emit("BUILD_LIST", buffered, line)
            self.emit("LIST_EXTEND", line=line)

        self.emit("BUILD_DICT", 0, line)
        buffered = 0
        for keyword in node.keywords:
            if keyword.arg is None:
                if buffered:
                    self.emit("BUILD_DICT", buffered, line)
                    self.emit("DICT_MERGE", line=line)
                    buffered = 0
                self.expression(keyword.value)
                self.emit("DICT_MERGE", line=line)
            else:
                self.emit("CONST", {"kind": "str", "value": keyword.arg}, line)
                self.expression(keyword.value)
                buffered += 1
        if buffered:
            self.emit("BUILD_DICT", buffered, line)
            self.emit("DICT_MERGE", line=line)

        self.emit("CALL_EX", line=line)

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
        # function_id -> list of semantic sites, parallel to that function's
        # `code`. Kept beside the IR rather than inside it, so enabling
        # annotation cannot change a single byte of a produced image.
        self.sites: dict[str, list[Any]] = {}
        self.imports: set[str] = set()

    def compile(self) -> dict[str, Any]:
        try:
            tree = ast.parse(self.source, filename=self.source_name)
        except SyntaxError as exc:
            raise CompileError(str(exc)) from exc
        module_scope = build_scope(tree.body, [], None, None, False)
        resolve_scope(module_scope)
        self.scopes = {}
        self._index_scopes(module_scope)
        module_names = collect_local_names(tree.body, [])
        module = FunctionCompiler(
            self,
            "__module__",
            "__module__",
            module_names,
            scope=module_scope,
        )
        module.statements(tree.body)
        module.emit("CONST", {"kind": "none"}, line=len(self.source.splitlines()) or 1)
        module.emit("RETURN", line=len(self.source.splitlines()) or 1)
        self.functions["__module__"] = {
            "id": "__module__",
            "name": "__module__",
            "params": [],
            "posonly_count": 0,
            "vararg": None,
            "kwonly": [],
            "kwarg": None,
            "default_count": 0,
            "kw_default_names": [],
            "cellvars": [],
            "freevars": [],
            "local_names": sorted(module.local_names),
            "code": module.code,
        }
        self.sites["__module__"] = module.sites
        return {
            "ir_version": IR_VERSION,
            "source_name": self.source_name,
            "source_sha256": hashlib.sha256(self.source.encode("utf-8")).hexdigest(),
            "entry_function": "__module__",
            "imports": sorted(self.imports),
            "functions": self.functions,
        }

    def _index_scopes(self, scope: Scope) -> None:
        for child in scope.children:
            self.scopes[id(child.node)] = child
            self._index_scopes(child)

    def compile_function(
        self, node: ast.FunctionDef, parent: FunctionCompiler
    ) -> tuple[str, list[str]]:
        if node.decorator_list:
            raise CompileError(
                f"{self.source_name}:{node.lineno}: decorators are unsupported"
            )
        args = node.args
        # `params` is every parameter that can be filled positionally, with the
        # positional-only ones first, matching the order CPython binds them.
        parameters = [arg.arg for arg in (*args.posonlyargs, *args.args)]
        vararg = args.vararg.arg if args.vararg else None
        keyword_only = [arg.arg for arg in args.kwonlyargs]
        kwarg = args.kwarg.arg if args.kwarg else None
        keyword_default_names = [
            arg.arg
            for arg, default in zip(args.kwonlyargs, args.kw_defaults)
            if default is not None
        ]
        bound = [*parameters, *keyword_only]
        if vararg:
            bound.append(vararg)
        if kwarg:
            bound.append(kwarg)
        if len(set(bound)) != len(bound):
            raise CompileError(
                f"{self.source_name}:{node.lineno}: duplicate parameter name"
            )
        local_names = collect_local_names(node.body, bound)
        function_id = f"{parent.name}.{node.name}@{node.lineno}"
        enclosing_locals = parent.enclosing_locals
        if parent.function_id != "__module__":
            enclosing_locals = (*enclosing_locals, parent.local_names)
        scope = self.scopes[id(node)]
        compiler = FunctionCompiler(
            self,
            function_id,
            node.name,
            local_names,
            enclosing_locals,
            scope=scope,
        )
        compiler.statements(node.body)
        compiler.emit("CONST", {"kind": "none"}, node.end_lineno or node.lineno)
        compiler.emit("RETURN", line=node.end_lineno or node.lineno)
        self.functions[function_id] = {
            "id": function_id,
            "name": node.name,
            "params": parameters,
            "posonly_count": len(args.posonlyargs),
            "vararg": vararg,
            "kwonly": keyword_only,
            "kwarg": kwarg,
            "default_count": len(args.defaults),
            "kw_default_names": keyword_default_names,
            "cellvars": sorted(compiler.cellvars),
            "freevars": list(compiler.freevars),
            "local_names": sorted(local_names),
            "code": compiler.code,
        }
        self.sites[function_id] = compiler.sites
        return function_id, list(compiler.freevars)


def compile_source(source: str, source_name: str) -> dict[str, Any]:
    return ProgramCompiler(source, source_name).compile()


def compile_with_sites(
    source: str, source_name: str
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Compile, and also return the semantic site of every instruction.

    The IR returned here is identical to `compile_source`'s -- the sites live in
    a separate table. That is what allows an image written by an older runtime,
    with no notion of semantic identity, to be annotated after the fact by
    recompiling the source it already carries.
    """

    compiler = ProgramCompiler(source, source_name)
    ir = compiler.compile()
    return ir, compiler.sites
