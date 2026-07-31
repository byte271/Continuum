"""Semantic identities for execution state, stable across source revisions.

Continuum's IR names a function `outer.bump@9` and a resume point by an integer
index into that function's code array. Both are adequate inside one revision and
useless across two: inserting a blank line renames every function below it, and
inserting an instruction renumbers every program counter after it. Mapping live
execution state from one source revision to another needs identities that
survive edits which do not change meaning, and that *stop* matching when meaning
does change.

Four identities are defined here:

`SemanticFunctionID`
    which function this is, by lexical scope path and declaration shape

`SemanticBindingID`
    which binding this is, by owning function, name, and binding kind

`SemanticControlRegionID`
    which loop, handler, or finally region execution is inside

`SemanticSafepointID`
    which resume point execution is at, by region path and statement identity

None is defined solely by a source line number, an AST child index, a function
display name, an integer program counter, a source hash, text similarity, or
edit distance. Each is a composite of several pieces of structural evidence, and
the composition is what makes it meaningful:

* A function is identified by its scope path *and* its full signature *and* the
  names it captures. A renamed parameter is a different function, so an active
  frame will not silently bind to it.
* A statement is keyed by a digest of its own shape *and* by how many statements
  of that same shape precede it in its body. Inserting a statement of a
  different shape anywhere therefore leaves surrounding identities untouched,
  which is what makes "add code after the resume point" a mappable edit.
* A compound statement's digest covers only its header. Editing a loop body does
  not change the identity of the loop, so an active loop keeps its identity
  while the code inside it changes.

Two identically shaped siblings do shift each other's occurrence index. That is
a genuine ambiguity, and it is reported as one rather than resolved by guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .compiler import compile_with_sites
from .errors import ContinuumError

SEMANTIC_MODEL_VERSION = "1.0"


class SemanticAmbiguity(ContinuumError):
    """Two distinct program elements claim one semantic identity.

    Always a refusal. If an active element is ambiguous there is no single
    correct place to resume, and picking one would be a guess.
    """


def _digest(kind: str, payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True)
class FunctionIdentity:
    """A function's semantic identity plus the evidence behind it.

    The evidence is retained so an accepted mapping can be explained rather
    than merely asserted.
    """

    semantic_id: str
    ir_function_id: str
    scope_path: tuple[str, ...]
    signature: dict[str, Any]
    freevars: tuple[str, ...]
    cellvars: tuple[str, ...]

    def evidence(self) -> dict[str, Any]:
        return {
            "semantic_function_id": self.semantic_id,
            "ir_function_id": self.ir_function_id,
            "scope_path": list(self.scope_path),
            "signature": self.signature,
            "captured_bindings": list(self.freevars),
            "provided_cells": list(self.cellvars),
        }


@dataclass(frozen=True)
class SafepointIdentity:
    semantic_id: str
    function: str
    pc: int
    region_path: tuple[Any, ...]
    statement_key: Any
    op: str

    def evidence(self) -> dict[str, Any]:
        return {
            "semantic_safepoint_id": self.semantic_id,
            "semantic_function_id": self.function,
            "ir_program_counter": self.pc,
            "opcode": self.op,
            "control_region_path": [list(entry) for entry in self.region_path],
            "statement_key": list(self.statement_key) if self.statement_key else None,
        }


@dataclass
class ProgramSemantics:
    """Every semantic identity in one source revision."""

    source: str
    source_name: str
    ir: dict[str, Any]
    functions: dict[str, FunctionIdentity] = field(default_factory=dict)
    by_ir_id: dict[str, FunctionIdentity] = field(default_factory=dict)
    ambiguous_functions: dict[str, list[str]] = field(default_factory=dict)
    # (semantic_function_id, semantic_safepoint_id) -> [pc, ...]
    safepoints: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    safepoint_by_pc: dict[tuple[str, int], SafepointIdentity] = field(
        default_factory=dict
    )
    bindings: dict[str, set[str]] = field(default_factory=dict)

    def function_for_ir_id(self, ir_function_id: str) -> FunctionIdentity | None:
        return self.by_ir_id.get(ir_function_id)

    def safepoint_at(
        self, ir_function_id: str, pc: int
    ) -> SafepointIdentity | None:
        return self.safepoint_by_pc.get((ir_function_id, pc))

    def resolve_safepoint(
        self, semantic_function_id: str, semantic_safepoint_id: str
    ) -> list[int]:
        return list(
            self.safepoints.get((semantic_function_id, semantic_safepoint_id), [])
        )

    def binding_ids(self, semantic_function_id: str) -> set[str]:
        return self.bindings.get(semantic_function_id, set())


def binding_identity(semantic_function_id: str, name: str, kind: str) -> str:
    """Identity of one lexical binding.

    Kind is part of the identity: a name that changes from an ordinary local to
    a captured cell is not the same binding, because the storage the resumed
    frame would need is different.
    """

    return _digest(
        "sbd",
        {"function": semantic_function_id, "name": name, "kind": kind},
    )


def control_region_identity(
    semantic_function_id: str, region_path: tuple[Any, ...]
) -> str:
    return _digest(
        "scr",
        {
            "function": semantic_function_id,
            "path": [list(entry) for entry in region_path],
        },
    )


def _scope_path(ir: dict[str, Any], ir_function_id: str) -> tuple[str, ...]:
    """Lexical nesting chain, taken from the IR's qualified identifiers.

    The IR encodes a function as `parent.name@line`. The line component is
    dropped here: it is exactly the part that is not stable across revisions.
    What remains is the chain of enclosing scope names, which is structural.
    """

    if ir_function_id == "__module__":
        return ("__module__",)
    qualified = ir_function_id.split("@", 1)[0]
    return tuple(qualified.split("."))


def analyze(source: str, source_name: str) -> ProgramSemantics:
    """Compute every semantic identity for one source revision."""

    ir, sites = compile_with_sites(source, source_name)
    semantics = ProgramSemantics(source=source, source_name=source_name, ir=ir)

    claims: dict[str, list[str]] = {}
    for ir_function_id, definition in sorted(ir["functions"].items()):
        signature = {
            "params": list(definition["params"]),
            "posonly_count": definition.get("posonly_count", 0),
            "vararg": definition.get("vararg"),
            "kwonly": list(definition.get("kwonly", [])),
            "kwarg": definition.get("kwarg"),
            "default_count": definition.get("default_count", 0),
            "kw_default_names": list(definition.get("kw_default_names", [])),
        }
        scope_path = _scope_path(ir, ir_function_id)
        freevars = tuple(definition.get("freevars", []))
        cellvars = tuple(definition.get("cellvars", []))
        semantic_id = _digest(
            "sfn",
            {
                "scope_path": list(scope_path),
                "signature": signature,
                "freevars": list(freevars),
                "cellvars": list(cellvars),
            },
        )
        identity = FunctionIdentity(
            semantic_id=semantic_id,
            ir_function_id=ir_function_id,
            scope_path=scope_path,
            signature=signature,
            freevars=freevars,
            cellvars=cellvars,
        )
        claims.setdefault(semantic_id, []).append(ir_function_id)
        semantics.functions[semantic_id] = identity
        semantics.by_ir_id[ir_function_id] = identity

        local_names = set(definition.get("local_names", []))
        cell_set = set(cellvars)
        free_set = set(freevars)
        ids: set[str] = set()
        for name in sorted(local_names | cell_set | free_set):
            if name in free_set:
                kind = "freevar"
            elif name in cell_set:
                kind = "cell"
            elif name in signature["params"] or name in signature["kwonly"]:
                kind = "parameter"
            else:
                kind = "local"
            ids.add(binding_identity(semantic_id, name, kind))
        semantics.bindings[semantic_id] = ids

        for pc, site in enumerate(sites[ir_function_id]):
            region_path, statement_key = site
            safepoint_id = _digest(
                "ssp",
                {
                    "region": [list(entry) for entry in region_path],
                    "statement": list(statement_key) if statement_key else None,
                    "op": definition["code"][pc]["op"],
                    "offset_in_statement": _offset_within_statement(
                        sites[ir_function_id], pc
                    ),
                },
            )
            identity_at_pc = SafepointIdentity(
                semantic_id=safepoint_id,
                function=semantic_id,
                pc=pc,
                region_path=tuple(region_path),
                statement_key=statement_key,
                op=definition["code"][pc]["op"],
            )
            semantics.safepoint_by_pc[(ir_function_id, pc)] = identity_at_pc
            semantics.safepoints.setdefault((semantic_id, safepoint_id), []).append(pc)

    semantics.ambiguous_functions = {
        semantic_id: sorted(names)
        for semantic_id, names in claims.items()
        if len(names) > 1
    }
    return semantics


def _offset_within_statement(sites: list[Any], pc: int) -> int:
    """How many instructions of the same statement precede this one.

    Two instructions of one statement share a region path and statement key, so
    without this a resume point in the middle of a statement would be
    indistinguishable from its neighbours. Counting backwards to the start of
    the current statement keeps the distinction while staying independent of
    the function's absolute instruction numbering.
    """

    site = sites[pc]
    offset = 0
    index = pc - 1
    while index >= 0 and sites[index] == site:
        offset += 1
        index -= 1
    return offset


def analyze_image_source(source: str, source_name: str, ir: dict[str, Any]) -> ProgramSemantics:
    """Analyze the source an image carries, proving it produced that image's IR.

    Recompiling the stored source must reproduce the stored IR exactly. If it
    does not, the annotation would describe a different program than the one the
    frames index into, and every mapping built on it would be meaningless.
    """

    semantics = analyze(source, source_name)
    produced = json.dumps(semantics.ir, sort_keys=True, separators=(",", ":"))
    stored = json.dumps(ir, sort_keys=True, separators=(",", ":"))
    if produced != stored:
        raise SemanticAmbiguity(
            "recompiling the image's stored source did not reproduce its stored "
            "IR; the image's source and IR disagree, so no semantic mapping can "
            "be trusted"
        )
    return semantics


__all__ = [
    "FunctionIdentity",
    "ProgramSemantics",
    "SEMANTIC_MODEL_VERSION",
    "SafepointIdentity",
    "SemanticAmbiguity",
    "analyze",
    "analyze_image_source",
    "binding_identity",
    "control_region_identity",
]
