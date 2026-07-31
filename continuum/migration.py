"""Plan, verify, and apply a migration of live execution state to new source.

Given an immutable image made from source revision A and a source revision B,
this produces either a **total** mapping of every active piece of execution
state from A to B, or an explicit refusal naming the exact element that could
not be mapped. There is no partial mapping: a plan either covers everything
live or it does not exist.

The mapping is built from the semantic identities in `continuum.semantics`, not
from line numbers, program counters, or text similarity. Every accepted mapping
carries the evidence that justified it, so a plan can be audited rather than
trusted.

The original image is never modified. Applying a plan rewrites only the
in-memory IR and frame positions of a restored VM; the `.cont` file on disk is
opened read-only and stays byte-identical.

Refusal is the default. Anything this module cannot prove safe is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .abi import EXECUTION_ABI_VERSION
from .codec import decode_graph
from .errors import ContinuumError
from .semantics import (
    SEMANTIC_MODEL_VERSION,
    ProgramSemantics,
    analyze,
    analyze_image_source,
    binding_identity,
    control_region_identity,
)
from .values import ClassValue, InstanceValue

PLAN_FORMAT_VERSION = "1.0"
PLAN_ENTRIES = ("plan.json", "new_source.py", "new_ir.json", "checksums.json")

# Refusal reason codes. Every one names a specific unmappable element.
REFUSE_ACTIVE_FUNCTION_MISSING = "active-function-missing"
REFUSE_ACTIVE_FUNCTION_AMBIGUOUS = "active-function-ambiguous"
REFUSE_SAFEPOINT_UNMAPPABLE = "active-safepoint-unmappable"
REFUSE_SAFEPOINT_AMBIGUOUS = "active-safepoint-ambiguous"
REFUSE_STACK_SHAPE_CHANGED = "operand-stack-shape-changed"
REFUSE_BINDING_MISSING = "active-binding-missing"
REFUSE_CELL_MISSING = "active-closure-cell-missing"
REFUSE_CONTROL_REGION_UNMAPPABLE = "active-control-region-unmappable"
REFUSE_CONTROL_REGION_AMBIGUOUS = "active-control-region-ambiguous"
REFUSE_CLASS_LAYOUT_CHANGED = "class-layout-changed"
REFUSE_CLASS_AMBIGUOUS = "class-ambiguous"
REFUSE_IMAGE_HASH_MISMATCH = "image-hash-mismatch"
REFUSE_SOURCE_HASH_MISMATCH = "source-hash-mismatch"
REFUSE_IR_HASH_MISMATCH = "ir-hash-mismatch"
REFUSE_PLAN_TAMPERED = "plan-tampered"
REFUSE_UNKNOWN_PLAN_VERSION = "unknown-plan-version"
REFUSE_UNKNOWN_EXECUTION_ABI = "unknown-execution-abi"
REFUSE_SOURCE_UNCHANGED_MISMATCH = "image-source-disagrees-with-ir"
REFUSE_MALFORMED_PLAN = "malformed-plan"
REFUSE_LIVE_FUNCTION_MISSING = "live-function-value-missing"

# Names the VM injects into the module frame. They are runtime-provided rather
# than declared by the source, so they have no semantic binding identity and
# are supplied identically by both revisions.
RUNTIME_INJECTED_NAMES = frozenset({"__name__", "__file__", "__args__"})


class MigrationRefused(ContinuumError):
    """A migration was refused. Carries the reason and the exact element."""

    def __init__(self, reason: str, element: str, detail: str):
        super().__init__(f"{detail} [{reason}: {element}]")
        self.reason = reason
        self.element = element
        self.detail = detail


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _describe_frame(loaded_ir: dict[str, Any], frame: dict[str, Any]) -> str:
    definition = loaded_ir["functions"].get(frame["function_id"], {})
    return f"{definition.get('name', frame['function_id'])}@pc{frame['pc']}"


def _map_point(
    old: ProgramSemantics,
    new: ProgramSemantics,
    old_ir_function_id: str,
    new_ir_function_id: str,
    semantic_function_id: str,
    pc: int,
    element: str,
) -> tuple[int, dict[str, Any]]:
    """Map one program counter, refusing anything that is not one-to-one."""

    point = old.safepoint_at(old_ir_function_id, pc)
    if point is None:
        raise MigrationRefused(
            REFUSE_SAFEPOINT_UNMAPPABLE,
            element,
            f"no semantic identity exists for program counter {pc}",
        )
    matches = new.resolve_safepoint(semantic_function_id, point.semantic_id)
    if not matches:
        raise MigrationRefused(
            REFUSE_SAFEPOINT_UNMAPPABLE,
            element,
            "the new revision has no location with this semantic identity; the "
            "active location was changed, removed, or moved across a "
            "control-flow boundary",
        )
    if len(matches) > 1:
        raise MigrationRefused(
            REFUSE_SAFEPOINT_AMBIGUOUS,
            element,
            f"one old location maps to {len(matches)} new locations",
        )
    new_pc = matches[0]

    # The instructions of the enclosing statement executed so far must be
    # identical, or the operand stack the frame carries would not correspond to
    # what the new code expects at that point.
    before = old.statement_prefix.get((old_ir_function_id, pc))
    after = new.statement_prefix.get((new_ir_function_id, new_pc))
    if before != after:
        raise MigrationRefused(
            REFUSE_STACK_SHAPE_CHANGED,
            element,
            "the operand-stack work performed within the active statement "
            f"differs: {list(before or ())} -> {list(after or ())}",
        )
    return new_pc, point.evidence()


def _live_classes(loaded: Any) -> dict[str, tuple[str, ...]]:
    """Every VM-owned class reachable from the frozen graph, with its members.

    Reading the graph is necessary: a class that no live instance refers to is
    free to change, and one that instances *do* refer to is not.
    """

    placeholders = {
        record["resource_id"]: object()
        for record in loaded.resources_document["resources"]
    }
    state = decode_graph(loaded.heap, placeholders)
    found: dict[str, tuple[str, ...]] = {}
    seen: set[int] = set()

    def walk(value: Any) -> None:
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, ClassValue):
            found[value.class_id] = tuple(sorted(value.members))
            for member in value.members.values():
                walk(member)
        elif isinstance(value, InstanceValue):
            walk(value.cls)
            for attribute in value.attributes.values():
                walk(attribute)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                walk(item)

    walk(state)
    return found


def plan_upgrade(
    image_path: str | os.PathLike[str],
    new_source_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Build a total migration plan, or refuse with the exact blocking element."""

    from .image import load_image

    image_path = Path(image_path)
    loaded = load_image(image_path)
    loaded.validate_compatibility()

    old_source = loaded.source
    old_ir = loaded.ir
    new_source = Path(new_source_path).read_text(encoding="utf-8")

    # Proves the annotation describes the same program the frames index into.
    old = analyze_image_source(old_source, old_ir["source_name"], old_ir)
    new = analyze(new_source, old_ir["source_name"])

    frames = loaded.frames_document["frames"]
    frame_mappings: list[dict[str, Any]] = []
    binding_mappings: list[dict[str, Any]] = []
    region_mappings: list[dict[str, Any]] = []

    live_frames = _load_live_frames(loaded)

    # Pass one: resolve every active frame's function first. Checking bindings
    # or resume points before this would report an incidental symptom of a
    # deleted active function instead of the deletion itself.
    resolved: list[tuple[Any, Any]] = []
    for depth, live in enumerate(live_frames):
        element = f"frame {depth} ({_describe_frame(old_ir, live)})"
        resolved.append(_resolve_function(old, new, live, element))

    # Class layout is checked before individual function values, because a
    # renamed or removed method shows up as both, and "this class's layout
    # changed" is the diagnosis an author can act on.
    class_mappings = _map_classes(loaded, old, new)

    # Every live function value must still exist too, not only the functions
    # with a frame on the stack. A closure held in a local will be called after
    # the resume, so if its identity changed the call would bind to the wrong
    # code or to nothing at all.
    _check_live_functions(loaded, old, new)

    # Live values hold IR identifiers, and those identifiers embed a line
    # number, so an edit anywhere above a function renames it. Record the full
    # old-to-new mapping; apply_plan rewrites every reachable value with it.
    function_id_mapping = {
        identity.ir_function_id: new.functions[semantic_id].ir_function_id
        for semantic_id, identity in old.functions.items()
        if semantic_id in new.functions
    }

    for depth, live in enumerate(live_frames):
        old_ir_function_id = live["function_id"]
        element = f"frame {depth} ({_describe_frame(old_ir, live)})"

        old_function, new_function = resolved[depth]
        semantic_id = old_function.semantic_id

        new_pc, point_evidence = _map_point(
            old,
            new,
            old_ir_function_id,
            new_function.ir_function_id,
            semantic_id,
            live["pc"],
            element,
        )

        available = new.binding_ids(semantic_id)
        for name in sorted(set(live["locals"]) - RUNTIME_INJECTED_NAMES):
            kind = _binding_kind(old_function, name, cell=False)
            identity = binding_identity(semantic_id, name, kind)
            if identity not in available:
                raise MigrationRefused(
                    REFUSE_BINDING_MISSING,
                    f"{element} binding {name!r}",
                    "an active local binding is absent from the new revision, "
                    "or changed kind",
                )
            binding_mappings.append(
                {
                    "frame_depth": depth,
                    "name": name,
                    "kind": kind,
                    "semantic_binding_id": identity,
                }
            )
        for name in sorted(live["cells"]):
            kind = _binding_kind(old_function, name, cell=True)
            identity = binding_identity(semantic_id, name, kind)
            if identity not in available:
                raise MigrationRefused(
                    REFUSE_CELL_MISSING,
                    f"{element} closure cell {name!r}",
                    "an active closure cell is absent from the new revision, "
                    "or changed kind",
                )
            binding_mappings.append(
                {
                    "frame_depth": depth,
                    "name": name,
                    "kind": kind,
                    "semantic_binding_id": identity,
                }
            )

        mapped_blocks = []
        for index, block in enumerate(live["blocks"]):
            block_element = f"{element} control block {index} ({block['kind']})"
            target, target_evidence = _map_point(
                old,
                new,
                old_ir_function_id,
                new_function.ir_function_id,
                semantic_id,
                block["target"],
                block_element,
            )
            region = old.safepoint_at(old_ir_function_id, block["target"])
            region_id = control_region_identity(
                semantic_id, region.region_path if region else ()
            )
            mapped_blocks.append(
                {
                    "index": index,
                    "kind": block["kind"],
                    "old_target": block["target"],
                    "new_target": target,
                    "stack_depth": block["stack_depth"],
                    "semantic_control_region_id": region_id,
                }
            )
            region_mappings.append(
                {
                    "frame_depth": depth,
                    "kind": block["kind"],
                    "semantic_control_region_id": region_id,
                    "evidence": target_evidence,
                }
            )

        frame_mappings.append(
            {
                "frame_depth": depth,
                "semantic_function_id": semantic_id,
                "old_ir_function_id": old_ir_function_id,
                "new_ir_function_id": new_function.ir_function_id,
                "old_pc": live["pc"],
                "new_pc": new_pc,
                "operand_stack_depth": len(live["stack"]),
                "pending_finally_reasons": len(live["finally_reasons"]),
                "control_blocks": mapped_blocks,
                "evidence": {
                    "old_function": old_function.evidence(),
                    "new_function": new_function.evidence(),
                    "resume_point": point_evidence,
                },
            }
        )

    new_ir = new.ir
    plan = {
        "plan_format_version": PLAN_FORMAT_VERSION,
        "semantic_model_version": SEMANTIC_MODEL_VERSION,
        "execution_abi_version": EXECUTION_ABI_VERSION,
        "created_by": __version__,
        "original_image_sha256": sha256_file(image_path),
        "old_source_sha256": _sha256(old_source.encode("utf-8")),
        "old_ir_sha256": _sha256(_json_bytes(old_ir)),
        "new_source_sha256": _sha256(new_source.encode("utf-8")),
        "new_ir_sha256": _sha256(_json_bytes(new_ir)),
        "entry_program": old_ir["source_name"],
        "active_frames": len(frame_mappings),
        "frame_mappings": frame_mappings,
        "binding_mappings": binding_mappings,
        "control_region_mappings": region_mappings,
        "class_mappings": class_mappings,
        "function_id_mappings": dict(sorted(function_id_mapping.items())),
        "accepted_edit_classes": _classify_edits(old, new, frame_mappings),
        "assumptions": [
            "The new revision's IR replaces the old one wholesale; only the "
            "active frames' positions are remapped.",
            "Completed effects are not re-executed and are not re-checked; the "
            "plan cannot undo work the old revision already performed.",
            "Inactive functions may differ arbitrarily; only functions with an "
            "active frame are mapped.",
            "Resource policy is unchanged; the plan does not migrate resource "
            "semantics.",
        ],
        "mapping_is_total": True,
    }
    return plan


def _resolve_function(
    old: ProgramSemantics, new: ProgramSemantics, live: dict[str, Any], element: str
) -> tuple[Any, Any]:
    """Find the new revision's counterpart of one active frame's function."""

    old_function = old.function_for_ir_id(live["function_id"])
    if old_function is None:
        raise MigrationRefused(
            REFUSE_ACTIVE_FUNCTION_MISSING,
            element,
            "the image references a function absent from its own source",
        )
    semantic_id = old_function.semantic_id
    if semantic_id in old.ambiguous_functions:
        raise MigrationRefused(
            REFUSE_ACTIVE_FUNCTION_AMBIGUOUS,
            element,
            "the old revision already has duplicate structures claiming this "
            f"identity: {old.ambiguous_functions[semantic_id]}",
        )
    if semantic_id in new.ambiguous_functions:
        raise MigrationRefused(
            REFUSE_ACTIVE_FUNCTION_AMBIGUOUS,
            element,
            "the new revision has duplicate structures claiming this identity: "
            f"{new.ambiguous_functions[semantic_id]}",
        )
    new_function = new.functions.get(semantic_id)
    if new_function is None:
        raise MigrationRefused(
            REFUSE_ACTIVE_FUNCTION_MISSING,
            element,
            "the new revision has no function with this semantic identity; an "
            "active function was deleted, renamed, or had its signature or "
            "captured bindings changed",
        )
    return old_function, new_function


def _check_live_functions(
    loaded: Any, old: ProgramSemantics, new: ProgramSemantics
) -> None:
    """Every reachable function value must survive with its identity intact.

    A closure sitting in a local variable has no frame on the stack, but it will
    be called after the resume. If its captured bindings or signature changed it
    is a different function, and rebinding the old value to it would be exactly
    the silent corruption this module exists to prevent.
    """

    for function_id in sorted(_live_function_ids(loaded)):
        element = f"live function value {function_id}"
        old_function = old.by_ir_id.get(function_id)
        if old_function is None:
            raise MigrationRefused(
                REFUSE_ACTIVE_FUNCTION_MISSING,
                element,
                "a live function value refers to code absent from the image's "
                "own source",
            )
        semantic_id = old_function.semantic_id
        if semantic_id in new.ambiguous_functions:
            raise MigrationRefused(
                REFUSE_ACTIVE_FUNCTION_AMBIGUOUS,
                element,
                "the new revision has duplicate structures claiming this identity",
            )
        if semantic_id not in new.functions:
            raise MigrationRefused(
                REFUSE_LIVE_FUNCTION_MISSING,
                element,
                "a live function value has no counterpart in the new revision; "
                "its signature or its captured bindings changed",
            )


def _live_function_ids(loaded: Any) -> set[str]:
    from .values import BoundMethodValue, FunctionValue

    placeholders = {
        record["resource_id"]: object()
        for record in loaded.resources_document["resources"]
    }
    state = decode_graph(loaded.heap, placeholders)
    found: set[str] = set()
    seen: set[int] = set()

    def walk(value: Any) -> None:
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, FunctionValue):
            found.add(value.function_id)
            for cell in value.closure:
                walk(cell)
            for item in (*value.defaults, *value.kw_defaults):
                walk(item)
        elif isinstance(value, BoundMethodValue):
            walk(value.function)
            walk(value.instance)
        elif isinstance(value, ClassValue):
            for member in value.members.values():
                walk(member)
        elif isinstance(value, InstanceValue):
            walk(value.cls)
            for attribute in value.attributes.values():
                walk(attribute)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                walk(item)

    walk(state)
    return found


def _binding_kind(function: Any, name: str, cell: bool) -> str:
    if name in function.freevars:
        return "freevar"
    if cell or name in function.cellvars:
        return "cell"
    if name in function.signature["params"] or name in function.signature["kwonly"]:
        return "parameter"
    return "local"


def _load_live_frames(loaded: Any) -> list[dict[str, Any]]:
    """Read the frozen frames as plain data, without starting execution."""

    placeholders = {
        record["resource_id"]: object()
        for record in loaded.resources_document["resources"]
    }
    state = decode_graph(loaded.heap, placeholders)
    result = []
    for frame in state["frames"]:
        result.append(
            {
                "function_id": frame["function_id"],
                "pc": frame["pc"],
                "locals": dict(frame["locals"]),
                "cells": dict(frame.get("cells", {})),
                "stack": list(frame["stack"]),
                "blocks": [dict(block) for block in frame["blocks"]],
                "finally_reasons": list(frame["finally_reasons"]),
            }
        )
    return result


def _map_classes(
    loaded: Any, old: ProgramSemantics, new: ProgramSemantics
) -> list[dict[str, Any]]:
    mappings = []
    for ir_class_id, members in sorted(_live_classes(loaded).items()):
        element = f"class {ir_class_id}"
        old_class = old.class_by_ir_id.get(ir_class_id)
        if old_class is None:
            raise MigrationRefused(
                REFUSE_CLASS_LAYOUT_CHANGED,
                element,
                "a live class is absent from the image's own source",
            )
        if old_class.semantic_id in old.ambiguous_classes:
            raise MigrationRefused(
                REFUSE_CLASS_AMBIGUOUS,
                element,
                "the old revision has duplicate classes claiming this identity",
            )
        if old_class.semantic_id in new.ambiguous_classes:
            raise MigrationRefused(
                REFUSE_CLASS_AMBIGUOUS,
                element,
                "the new revision has duplicate classes claiming this identity",
            )
        new_class = new.classes.get(old_class.semantic_id)
        if new_class is None:
            raise MigrationRefused(
                REFUSE_CLASS_LAYOUT_CHANGED,
                element,
                "the new revision has no class with this layout; a live "
                "instance's class gained, lost, renamed, or moved a member",
            )
        if tuple(sorted(new_class.members)) != tuple(sorted(members)):
            raise MigrationRefused(
                REFUSE_CLASS_LAYOUT_CHANGED,
                element,
                f"live instances were built against members {list(members)}, "
                f"the new revision declares {list(new_class.members)}",
            )
        mappings.append(
            {
                "semantic_class_id": old_class.semantic_id,
                "old_ir_class_id": old_class.ir_class_id,
                "new_ir_class_id": new_class.ir_class_id,
                "members": list(members),
                "evidence": {
                    "old_class": old_class.evidence(),
                    "new_class": new_class.evidence(),
                },
            }
        )
    return mappings


def _classify_edits(
    old: ProgramSemantics, new: ProgramSemantics, frame_mappings: list[dict[str, Any]]
) -> list[str]:
    """Name the edit classes this plan relies on, for the audit record."""

    classes = []
    active = {mapping["semantic_function_id"] for mapping in frame_mappings}
    old_ids = set(old.functions)
    new_ids = set(new.functions)
    if new_ids - old_ids:
        classes.append("added-future-only-functions")
    if (old_ids - new_ids) - active:
        classes.append("changed-or-removed-inactive-functions")
    if old.source != new.source:
        classes.append("changed-future-constants-or-expressions")
    for mapping in frame_mappings:
        if mapping["old_pc"] != mapping["new_pc"]:
            classes.append("inserted-statements-after-the-active-resume-point")
            break
    return sorted(set(classes))


def write_plan(path: str | os.PathLike[str], plan: dict[str, Any], new_source: str,
               new_ir: dict[str, Any]) -> None:
    """Write an immutable, checksummed migration artifact."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        "plan.json": _json_bytes(plan),
        "new_source.py": new_source.encode("utf-8"),
        "new_ir.json": _json_bytes(new_ir),
    }
    entries["checksums.json"] = _json_bytes(
        {
            "algorithm": "sha256",
            "entries": {
                name: _sha256(content) for name, content in sorted(entries.items())
            },
        }
    )
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp",
        dir=destination.parent, delete=False
    ) as temporary:
        temporary_name = temporary.name
    try:
        with zipfile.ZipFile(
            temporary_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
        with open(temporary_name, "r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


MAX_PLAN_ENTRIES = 64
MAX_PLAN_ENTRY_BYTES = 64 * 1024 * 1024
MAX_PLAN_RATIO = 500


def read_plan(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Read a plan under explicit bounds, verifying every entry checksum."""

    source_path = Path(path).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(source_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise MigrationRefused(
            REFUSE_MALFORMED_PLAN, str(source_path), f"not a migration plan: {exc}"
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PLAN_ENTRIES:
            raise MigrationRefused(
                REFUSE_MALFORMED_PLAN, "archive", "too many entries"
            )
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise MigrationRefused(
                REFUSE_PLAN_TAMPERED, "archive", "duplicate archive entries"
            )
        if set(names) != set(PLAN_ENTRIES):
            raise MigrationRefused(
                REFUSE_MALFORMED_PLAN,
                "archive",
                f"unexpected entry set: {sorted(names)}",
            )
        for info in infos:
            if info.file_size > MAX_PLAN_ENTRY_BYTES:
                raise MigrationRefused(
                    REFUSE_MALFORMED_PLAN, info.filename, "entry is too large"
                )
            if info.compress_size and (
                info.file_size / max(info.compress_size, 1) > MAX_PLAN_RATIO
            ):
                raise MigrationRefused(
                    REFUSE_MALFORMED_PLAN,
                    info.filename,
                    "entry compression ratio is implausible",
                )
        raw = {info.filename: archive.read(info) for info in infos}

    checksums = json.loads(raw["checksums.json"])
    for name, expected in sorted(checksums["entries"].items()):
        if name not in raw or _sha256(raw[name]) != expected:
            raise MigrationRefused(
                REFUSE_PLAN_TAMPERED, name, "plan entry checksum does not match"
            )
    covered = set(checksums["entries"])
    if covered != set(PLAN_ENTRIES) - {"checksums.json"}:
        raise MigrationRefused(
            REFUSE_PLAN_TAMPERED, "checksums.json", "checksum coverage is incomplete"
        )

    plan = json.loads(raw["plan.json"])
    if plan.get("plan_format_version") != PLAN_FORMAT_VERSION:
        raise MigrationRefused(
            REFUSE_UNKNOWN_PLAN_VERSION,
            "plan.json",
            f"plan format {plan.get('plan_format_version')!r} is not supported",
        )
    if plan.get("execution_abi_version") != EXECUTION_ABI_VERSION:
        raise MigrationRefused(
            REFUSE_UNKNOWN_EXECUTION_ABI,
            "plan.json",
            f"plan requires execution ABI {plan.get('execution_abi_version')!r}",
        )
    new_source = raw["new_source.py"].decode("utf-8")
    new_ir = json.loads(raw["new_ir.json"])
    if _sha256(raw["new_source.py"]) != plan.get("new_source_sha256"):
        raise MigrationRefused(
            REFUSE_SOURCE_HASH_MISMATCH, "new_source.py", "new source hash mismatch"
        )
    if _sha256(_json_bytes(new_ir)) != plan.get("new_ir_sha256"):
        raise MigrationRefused(
            REFUSE_IR_HASH_MISMATCH, "new_ir.json", "new IR hash mismatch"
        )
    return plan, new_source, new_ir


def verify_plan(
    image_path: str | os.PathLike[str], plan_path: str | os.PathLike[str]
) -> dict[str, Any]:
    """Independently re-derive the plan and confirm it matches, byte for byte.

    Verification does not trust the plan's mappings. It recomputes them from the
    image and the plan's own new source, and refuses if anything differs. It
    never executes the user program.
    """

    plan, new_source, new_ir = read_plan(plan_path)

    actual_image = sha256_file(image_path)
    if actual_image != plan.get("original_image_sha256"):
        raise MigrationRefused(
            REFUSE_IMAGE_HASH_MISMATCH,
            str(image_path),
            f"plan was made for image {plan.get('original_image_sha256')}, "
            f"this image is {actual_image}",
        )

    with tempfile.TemporaryDirectory() as temporary:
        candidate = Path(temporary) / "new_source.py"
        candidate.write_text(new_source, encoding="utf-8")
        recomputed = plan_upgrade(image_path, candidate)

    ignored = {"created_by"}
    left = {k: v for k, v in plan.items() if k not in ignored}
    right = {k: v for k, v in recomputed.items() if k not in ignored}
    if left != right:
        differing = sorted(
            key for key in set(left) | set(right) if left.get(key) != right.get(key)
        )
        raise MigrationRefused(
            REFUSE_PLAN_TAMPERED,
            ", ".join(differing) or "plan.json",
            "the plan does not match an independent re-derivation from the "
            "image and its declared new source",
        )
    return {
        "plan_format_version": plan["plan_format_version"],
        "execution_abi_version": plan["execution_abi_version"],
        "original_image_sha256": plan["original_image_sha256"],
        "new_source_sha256": plan["new_source_sha256"],
        "new_ir_sha256": plan["new_ir_sha256"],
        "active_frames": plan["active_frames"],
        "accepted_edit_classes": plan["accepted_edit_classes"],
        "mapping_is_total": plan["mapping_is_total"],
        "independently_rederived": True,
        "integrity": "verified",
        "execution": "not started",
    }


def apply_plan(vm: Any, plan: dict[str, Any], new_ir: dict[str, Any]) -> None:
    """Move a restored VM onto the new revision. In memory only.

    Called after the VM has been restored from the unmodified image. The image
    file is never written to.
    """

    mappings = {mapping["frame_depth"]: mapping for mapping in plan["frame_mappings"]}
    if len(mappings) != len(vm.frames):
        raise MigrationRefused(
            REFUSE_MALFORMED_PLAN,
            "frame_mappings",
            f"plan maps {len(mappings)} frames, the image restored "
            f"{len(vm.frames)}",
        )
    for depth, frame in enumerate(vm.frames):
        mapping = mappings.get(depth)
        if mapping is None or mapping["old_ir_function_id"] != frame.function_id:
            raise MigrationRefused(
                REFUSE_MALFORMED_PLAN,
                f"frame {depth}",
                "plan does not describe this frame",
            )
        if mapping["old_pc"] != frame.pc:
            raise MigrationRefused(
                REFUSE_MALFORMED_PLAN,
                f"frame {depth}",
                "plan was built for a different resume position",
            )
        frame.function_id = mapping["new_ir_function_id"]
        frame.pc = mapping["new_pc"]
        for block, mapped in zip(frame.blocks, mapping["control_blocks"]):
            block["target"] = mapped["new_target"]
    _rewrite_live_identifiers(vm, plan)
    vm.ir = new_ir
    vm._prepare_execution()
    vm._validate_state()


def _rewrite_live_identifiers(vm: Any, plan: dict[str, Any]) -> None:
    """Point every reachable value at its counterpart in the new revision.

    A frame's function is remapped by the caller, but a `FunctionValue` sitting
    in a global, a closure, or a class member carries its own IR identifier.
    Those identifiers embed a line number, so inserting a single line above a
    function renames it, and a value that still names the old identifier would
    fail to resolve -- or, worse, resolve to a different function that happened
    to inherit the name. Both are silent corruption, so identifiers are
    rewritten here rather than left to chance.

    `FunctionValue` is frozen, so the identifier is replaced in place rather
    than by rebuilding the value: rebuilding would break the sharing that makes
    two references to one closure the same object.
    """

    from .values import BoundMethodValue, FunctionValue

    functions = plan.get("function_id_mappings") or {}
    classes = {
        mapping["old_ir_class_id"]: mapping["new_ir_class_id"]
        for mapping in plan.get("class_mappings", [])
    }
    if not functions and not classes:
        return

    seen: set[int] = set()

    def walk(value: Any) -> None:
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, FunctionValue):
            replacement = functions.get(value.function_id)
            if replacement is None:
                raise MigrationRefused(
                    REFUSE_LIVE_FUNCTION_MISSING,
                    f"live function value {value.function_id}",
                    "no counterpart for a reachable function value",
                )
            if replacement != value.function_id:
                object.__setattr__(value, "function_id", replacement)
            for cell in value.closure:
                walk(cell)
            for item in (*value.defaults, *value.kw_defaults):
                walk(item)
        elif isinstance(value, ClassValue):
            replacement = classes.get(value.class_id)
            if replacement is not None and replacement != value.class_id:
                value.class_id = replacement
            for member in value.members.values():
                walk(member)
        elif isinstance(value, BoundMethodValue):
            walk(value.function)
            walk(value.instance)
        elif isinstance(value, InstanceValue):
            walk(value.cls)
            for attribute in value.attributes.values():
                walk(attribute)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                walk(item)

    walk(vm.globals)
    for frame in vm.frames:
        walk(frame.locals)
        walk(frame.stack)
        walk(frame.cells)
        walk(frame.blocks)
        walk(frame.finally_reasons)


__all__ = [
    "MigrationRefused",
    "PLAN_FORMAT_VERSION",
    "apply_plan",
    "plan_upgrade",
    "read_plan",
    "sha256_file",
    "verify_plan",
    "write_plan",
]
