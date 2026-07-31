#!/usr/bin/env python3
"""Paired cross-Python differential suite over the compatibility corpus.

For every accepted program and every reachable safe point this freezes live
execution under one interpreter, deeply verifies the image without executing it,
restores it under another interpreter, and compares the result against an
independently run uninterrupted control.

The comparison is not limited to output. Both sides compute a canonical
*state fingerprint* of the live VM, covering the logical frame chain, resume
positions, locals, lexical cells, operand stacks, control blocks, pending
finally reasons, module RNG state, `random.Random` instances, file offsets, and
— crucially — object identity. Identity is captured by labelling each object on
first visit and emitting a back-reference on revisit, so shared references and
reference cycles are part of the compared structure rather than something the
comparison has to be told about. Two fingerprints are equal only if the graph
shapes match, not merely the values.

Every case is classified. Refused and frontend-unsupported cases are reported
separately and never counted as successes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum.abi import IncompatibleImage  # noqa: E402
from continuum.compiler import compile_source  # noqa: E402
from continuum.errors import (  # noqa: E402
    CompileError,
    ContinuumError,
    ExecutionError,
    ImageError,
    UnsupportedObjectError,
)
from continuum.image import load_image, save_image, verify_image  # noqa: E402
from continuum.resources import PortableFile  # noqa: E402
from continuum.values import (  # noqa: E402
    EMPTY,
    BoundAttrRef,
    BoundMethodValue,
    BuiltinRef,
    Cell,
    ClassValue,
    FunctionValue,
    InstanceValue,
    ModuleAttrRef,
    ModuleRef,
    VMIterator,
)
from continuum.vm import VirtualMachine  # noqa: E402

CORPUS = REPOSITORY / "compatibility" / "programs"

# Classifications. Only ACCEPTED contributes to the correctness rate; a silent
# mismatch is the one outcome that must never occur.
ACCEPTED = "accepted-and-correct"
REFUSED = "explicitly-refused"
UNSUPPORTED = "unsupported-by-language-frontend"
INFRASTRUCTURE = "infrastructure-failure"
MISMATCH = "silent-mismatch"


class Fingerprinter:
    """Canonical, identity-preserving view of live VM state.

    Objects are labelled on first visit; a revisit emits `{"ref": label}`. That
    makes sharing and cycles structural features of the fingerprint, so a
    restore that duplicated a shared list or broke a cycle produces a different
    fingerprint even when every scalar value matches.
    """

    def __init__(self) -> None:
        self._labels: dict[int, str] = {}
        self._live: list[Any] = []

    def _label(self, value: Any) -> tuple[str, bool]:
        key = id(value)
        if key in self._labels:
            return self._labels[key], True
        label = f"o{len(self._labels)}"
        self._labels[key] = label
        # Hold a reference so no object is collected and its id reused while
        # the walk is still in progress.
        self._live.append(value)
        return label, False

    def walk(self, value: Any) -> Any:
        if isinstance(value, bytes):
            # Hex so the fingerprint stays JSON-serializable without losing a
            # single byte of the compared value.
            return {"t": "bytes", "v": value.hex()}
        if value is None or isinstance(value, (bool, int, float, str)):
            # Distinguish types that compare equal across types (1 == 1.0 ==
            # True) so a changed type is never invisible.
            return {"t": type(value).__name__, "v": value}
        if value is EMPTY:
            return {"t": "empty-cell"}

        label, seen = self._label(value)
        if seen:
            return {"ref": label}

        if isinstance(value, list):
            return {"t": "list", "id": label, "items": [self.walk(i) for i in value]}
        if isinstance(value, tuple):
            return {"t": "tuple", "id": label, "items": [self.walk(i) for i in value]}
        if isinstance(value, set):
            # Sets have no portable order; compare a sorted canonical form.
            return {
                "t": "set",
                "id": label,
                "items": sorted(
                    json.dumps(self.walk(i), sort_keys=True) for i in value
                ),
            }
        if isinstance(value, dict):
            # Insertion order is semantically observable in Python, so it is
            # compared rather than sorted away.
            return {
                "t": "dict",
                "id": label,
                "items": [[self.walk(k), self.walk(v)] for k, v in value.items()],
            }
        if isinstance(value, Cell):
            return {"t": "cell", "id": label, "value": self.walk(value.value)}
        if isinstance(value, FunctionValue):
            return {
                "t": "function",
                "id": label,
                "function_id": value.function_id,
                "defaults": [self.walk(i) for i in value.defaults],
                "kw_defaults": [self.walk(i) for i in value.kw_defaults],
                "closure": [self.walk(i) for i in value.closure],
            }
        if isinstance(value, ClassValue):
            return {
                "t": "class",
                "id": label,
                "class_id": value.class_id,
                "name": value.name,
                "members": [
                    [k, self.walk(v)] for k, v in sorted(value.members.items())
                ],
            }
        if isinstance(value, InstanceValue):
            return {
                "t": "instance",
                "id": label,
                "cls": self.walk(value.cls),
                "attributes": [
                    [k, self.walk(v)] for k, v in sorted(value.attributes.items())
                ],
            }
        if isinstance(value, BoundMethodValue):
            return {
                "t": "bound-method",
                "id": label,
                "instance": self.walk(value.instance),
                "function": self.walk(value.function),
            }
        if isinstance(value, VMIterator):
            return {
                "t": "iterator",
                "id": label,
                "index": value.index,
                "iterable": self.walk(value.iterable),
                "dict_keys": (
                    None
                    if value.dict_keys is None
                    else [self.walk(k) for k in value.dict_keys]
                ),
            }
        if isinstance(value, BuiltinRef):
            return {"t": "builtin", "name": value.name}
        if isinstance(value, ModuleRef):
            return {"t": "module", "name": value.name}
        if isinstance(value, ModuleAttrRef):
            return {"t": "module-attr", "module": value.module, "attr": value.attr}
        if isinstance(value, BoundAttrRef):
            return {
                "t": "bound-attr",
                "id": label,
                "receiver": self.walk(value.receiver),
                "attr": value.attr,
            }
        if isinstance(value, random.Random):
            # A Random instance's full MT19937 state, so a resumed generator
            # must produce the identical stream, not merely be a Random.
            return {"t": "random", "id": label, "state": self.walk(value.getstate())}
        if isinstance(value, PortableFile):
            return {
                "t": "file",
                "id": label,
                "path": str(value.path),
                "mode": value.mode,
                "offset": value.offset,
                "closed": value.closed,
            }
        if isinstance(value, BaseException):
            return {
                "t": "exception",
                "id": label,
                "type": type(value).__name__,
                "args": [self.walk(a) for a in value.args],
            }
        return {"t": "opaque", "id": label, "repr": type(value).__name__}

    def frame(self, vm: VirtualMachine, frame: Any, depth: int) -> dict[str, Any]:
        function = vm.ir["functions"][frame.function_id]
        instruction = function["code"][frame.pc]
        return {
            "depth": depth,
            "function_id": frame.function_id,
            "function_name": function["name"],
            "resume_pc": frame.pc,
            "resume_op": instruction["op"],
            "resume_line": instruction["line"],
            "locals": [
                [name, self.walk(value)]
                for name, value in sorted(frame.locals.items())
            ],
            "cells": [
                [name, self.walk(cell)] for name, cell in sorted(frame.cells.items())
            ],
            "operand_stack": [self.walk(item) for item in frame.stack],
            "control_blocks": [
                {
                    key: (self.walk(value) if key == "exception" else value)
                    for key, value in sorted(block.items())
                }
                for block in frame.blocks
            ],
            "finally_reasons": [
                {
                    key: (self.walk(value) if key in {"exception", "value"} else value)
                    for key, value in sorted(reason.items())
                }
                for reason in frame.finally_reasons
            ],
            "discard_result": frame.discard_result,
        }

    def vm_state(self, vm: VirtualMachine) -> dict[str, Any]:
        return {
            "frame_chain": [
                vm.ir["functions"][frame.function_id]["name"] for frame in vm.frames
            ],
            "frames": [
                self.frame(vm, frame, depth)
                for depth, frame in enumerate(vm.frames)
            ],
            "globals": [
                [name, self.walk(value)]
                for name, value in sorted(vm.globals.items())
                if name != "__args__"
            ],
            "module_random_state": self.walk(random.getstate()),
            "instructions_executed": vm.instructions_executed,
            "safe_points_executed": vm.safe_points_executed,
            "argv": list(vm.argv),
        }


def fingerprint(vm: VirtualMachine) -> dict[str, Any]:
    return Fingerprinter().vm_state(vm)


def build_vm(source: str, name: str) -> VirtualMachine:
    return VirtualMachine(compile_source(source, name), [name], name)


def run_control(source: str, name: str) -> dict[str, Any]:
    """Run the program to completion, uninterrupted, as the oracle."""

    saved = random.getstate()
    try:
        vm = build_vm(source, name)
        stream = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            result = vm.run()
        return {
            "stdout": stream.getvalue(),
            "stderr": errors.getvalue(),
            "result": repr(result),
            "safe_points": vm.safe_points_executed,
        }
    finally:
        random.setstate(saved)


def source_case(
    source: str, name: str, safe_point: int, image_path: Path
) -> dict[str, Any]:
    """Freeze at `safe_point` and record the pre-transfer fingerprint."""

    saved = random.getstate()
    try:
        vm = build_vm(source, name)
        stream = io.StringIO()
        errors = io.StringIO()
        # Step to the checkpoint rather than raising out of the safe-point
        # callback: an exception thrown from the callback is observable by the
        # running program's own handlers, which would change the very state this
        # is trying to capture.
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            while vm.frames and not vm.completed:
                if vm.safe_points_executed >= safe_point:
                    break
                vm.step()
        if vm.completed or not vm.frames:
            return {"status": "completed-before-safe-point"}

        state = fingerprint(vm)
        save_image(image_path, vm, source)
        return {
            "status": "frozen",
            "fingerprint": state,
            "stdout": stream.getvalue(),
            "stderr": errors.getvalue(),
        }
    finally:
        random.setstate(saved)


def target_case(image_path: Path) -> dict[str, Any]:
    """Deeply verify, then restore and finish, recording both fingerprints."""

    saved = random.getstate()
    try:
        # Verification must not execute the program. It runs before restore and
        # its report is retained so the ordering is visible in the evidence.
        verification = verify_image(image_path)
        loaded = load_image(image_path)
        vm = loaded.restore_vm()
        state = fingerprint(vm)
        stream = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            result = vm.run()
        return {
            "status": "restored",
            "fingerprint": state,
            "stdout": stream.getvalue(),
            "stderr": errors.getvalue(),
            "result": repr(result),
            "verification": {
                "integrity": verification["integrity"],
                "compatibility": verification["compatibility"],
                "policy": verification["execution_contract"]["compatibility_policy"],
            },
        }
    finally:
        random.setstate(saved)


# ---------------------------------------------------------------------------
# Worker protocol. The coordinator runs this file under each interpreter; the
# worker reads one JSON request on stdin and writes one JSON reply on stdout.
# ---------------------------------------------------------------------------


def worker() -> int:
    request = json.loads(sys.stdin.read())
    action = request["action"]
    try:
        if action == "control":
            payload = run_control(request["source"], request["name"])
        elif action == "source":
            payload = source_case(
                request["source"],
                request["name"],
                request["safe_point"],
                Path(request["image"]),
            )
        elif action == "target":
            payload = target_case(Path(request["image"]))
        elif action == "identity":
            import platform

            payload = {
                "python_version": platform.python_version(),
                "os": platform.system(),
                "machine": platform.machine(),
            }
        else:
            raise ValueError(f"unknown action {action!r}")
        print(json.dumps({"ok": True, "payload": payload}))
        return 0
    except (
        CompileError,
        UnsupportedObjectError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "kind": UNSUPPORTED, "error": f"{type(exc).__name__}: {exc}"}
            )
        )
        return 0
    except (IncompatibleImage, ImageError) as exc:
        print(
            json.dumps(
                {"ok": False, "kind": REFUSED, "error": f"{type(exc).__name__}: {exc}"}
            )
        )
        return 0
    except (ExecutionError, ContinuumError) as exc:
        print(
            json.dumps(
                {"ok": False, "kind": UNSUPPORTED, "error": f"{type(exc).__name__}: {exc}"}
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - reported as infrastructure
        import traceback

        print(
            json.dumps(
                {
                    "ok": False,
                    "kind": INFRASTRUCTURE,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        )
        return 0


def call(python: str, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        [python, str(Path(__file__).resolve()), "worker"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPOSITORY),
        env={**os.environ, "PYTHONPATH": str(REPOSITORY)},
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return {
            "ok": False,
            "kind": INFRASTRUCTURE,
            "error": (
                f"worker exited {completed.returncode}: "
                f"{completed.stderr.strip()[:2000]}"
            ),
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "kind": INFRASTRUCTURE, "error": f"bad worker reply: {exc}"}


# ---------------------------------------------------------------------------
# Coordinator.
# ---------------------------------------------------------------------------


def compare(
    source: dict[str, Any], target: dict[str, Any], control: dict[str, Any]
) -> list[str]:
    """Every semantic difference between a migrated run and the control.

    An empty list is the only accepted outcome. Each entry names the specific
    dimension that differed, so a failure identifies what broke rather than
    only that something did.
    """

    differences: list[str] = []
    source_state = source["fingerprint"]
    target_state = target["fingerprint"]

    if source_state["frame_chain"] != target_state["frame_chain"]:
        differences.append(
            f"frame chain: {source_state['frame_chain']} -> "
            f"{target_state['frame_chain']}"
        )
    if len(source_state["frames"]) != len(target_state["frames"]):
        differences.append("frame count changed across the crossing")
    else:
        for before, after in zip(source_state["frames"], target_state["frames"]):
            where = f"frame {before['depth']} ({before['function_name']})"
            if before["resume_pc"] != after["resume_pc"]:
                differences.append(f"{where}: resume position changed")
            if before["resume_op"] != after["resume_op"]:
                differences.append(f"{where}: resume opcode changed")
            if before["locals"] != after["locals"]:
                differences.append(f"{where}: locals changed")
            if before["cells"] != after["cells"]:
                differences.append(f"{where}: lexical cells changed")
            if before["operand_stack"] != after["operand_stack"]:
                differences.append(f"{where}: operand stack changed")
            if before["control_blocks"] != after["control_blocks"]:
                differences.append(f"{where}: control blocks changed")
            if before["finally_reasons"] != after["finally_reasons"]:
                differences.append(f"{where}: pending finally state changed")
            if before["discard_result"] != after["discard_result"]:
                differences.append(f"{where}: result disposition changed")

    if source_state["globals"] != target_state["globals"]:
        differences.append("module globals changed across the crossing")
    if source_state["module_random_state"] != target_state["module_random_state"]:
        differences.append("module RNG state changed across the crossing")
    if source_state["instructions_executed"] != target_state["instructions_executed"]:
        differences.append("instruction counter changed across the crossing")
    if source_state["safe_points_executed"] != target_state["safe_points_executed"]:
        differences.append("safe-point counter changed across the crossing")
    if source_state["argv"] != target_state["argv"]:
        differences.append("argv changed across the crossing")

    combined = source["stdout"] + target["stdout"]
    if combined != control["stdout"]:
        differences.append("source plus target stdout did not match the control")
    if not control["stdout"].startswith(source["stdout"]):
        differences.append("source stdout is not a prefix of the control stdout")
    if source["stderr"] + target["stderr"] != control["stderr"]:
        differences.append("source plus target stderr did not match the control")
    if target["result"] != control["result"]:
        differences.append(
            f"final result differed: {target['result']} != {control['result']}"
        )

    prefix_lines = [line for line in source["stdout"].splitlines() if line]
    suffix_lines = [line for line in target["stdout"].splitlines() if line]
    repeated = sorted(set(prefix_lines) & set(suffix_lines))
    if repeated:
        differences.append(f"completed actions repeated: {repeated[:5]}")

    return differences


def safe_points_for(total: int, count: int) -> list[int]:
    """Spread checkpoints across a run, always including its first and last.

    Sampling by execution position rather than by program feature keeps the
    corpus free of workload-specific knowledge.
    """

    if total <= 1:
        return []
    if total <= count:
        return list(range(1, total))
    stride = total / (count + 1)
    points = sorted({max(1, int(round(stride * (index + 1)))) for index in range(count)})
    return [point for point in points if 1 <= point < total]


def run_corpus(args: argparse.Namespace) -> int:
    programs = sorted(CORPUS.glob("*.py"))
    if args.program:
        wanted = set(args.program)
        programs = [path for path in programs if path.stem in wanted]
    if not programs:
        raise SystemExit("no corpus programs selected")

    source_identity = call(args.source_python, {"action": "identity"}, args.timeout)
    target_identity = call(args.target_python, {"action": "identity"}, args.timeout)
    if not source_identity.get("ok") or not target_identity.get("ok"):
        raise SystemExit("cannot identify the source or target interpreter")

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    cases: list[dict[str, Any]] = []
    started = time.monotonic()

    for path in programs:
        source_text = path.read_text(encoding="utf-8")
        name = path.name

        # The control runs on the target interpreter: the oracle for a migrated
        # run is what the program does when it is never interrupted at all.
        control = call(
            args.target_python,
            {"action": "control", "source": source_text, "name": name},
            args.timeout,
        )
        if not control.get("ok"):
            cases.append(
                {
                    "program": path.stem,
                    "safe_point": None,
                    "classification": control.get("kind", INFRASTRUCTURE),
                    "detail": control.get("error", ""),
                }
            )
            continue
        control_payload = control["payload"]
        total = control_payload["safe_points"]

        for safe_point in safe_points_for(total, args.checkpoints):
            image = workdir / f"{path.stem}-{safe_point}.cont"
            record: dict[str, Any] = {"program": path.stem, "safe_point": safe_point}

            frozen = call(
                args.source_python,
                {
                    "action": "source",
                    "source": source_text,
                    "name": name,
                    "safe_point": safe_point,
                    "image": str(image),
                },
                args.timeout,
            )
            if not frozen.get("ok"):
                record["classification"] = frozen.get("kind", INFRASTRUCTURE)
                record["detail"] = frozen.get("error", "")
                cases.append(record)
                continue
            source_payload = frozen["payload"]
            if source_payload["status"] != "frozen":
                record["classification"] = REFUSED
                record["detail"] = source_payload["status"]
                cases.append(record)
                image.unlink(missing_ok=True)
                continue

            restored = call(
                args.target_python,
                {"action": "target", "image": str(image)},
                args.timeout,
            )
            if not restored.get("ok"):
                record["classification"] = restored.get("kind", INFRASTRUCTURE)
                record["detail"] = restored.get("error", "")
                cases.append(record)
                image.unlink(missing_ok=True)
                continue

            differences = compare(source_payload, restored["payload"], control_payload)
            if differences:
                record["classification"] = MISMATCH
                record["differences"] = differences
            else:
                record["classification"] = ACCEPTED
                record["frames"] = len(source_payload["fingerprint"]["frames"])
                record["frame_chain"] = source_payload["fingerprint"]["frame_chain"]
            cases.append(record)
            if not args.keep_images:
                image.unlink(missing_ok=True)

    elapsed = time.monotonic() - started
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["classification"]] = counts.get(case["classification"], 0) + 1
    accepted = counts.get(ACCEPTED, 0)
    mismatches = counts.get(MISMATCH, 0)
    infrastructure = counts.get(INFRASTRUCTURE, 0)
    decided = accepted + mismatches

    report = {
        "source": source_identity["payload"],
        "target": target_identity["payload"],
        "cross_python": (
            source_identity["payload"]["python_version"]
            != target_identity["payload"]["python_version"]
        ),
        "programs": len(programs),
        "checkpoints_per_program": args.checkpoints,
        "cases": len(cases),
        "counts": counts,
        # Refusals and frontend gaps are reported separately and are never
        # folded into the correctness rate.
        "correctness_among_accepted_cases": (
            1.0 if decided == 0 else accepted / decided
        ),
        "silent_mismatches": mismatches,
        "infrastructure_failures": infrastructure,
        "elapsed_seconds": round(elapsed, 2),
        "case_records": cases,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    summary = {key: value for key, value in report.items() if key != "case_records"}
    print(json.dumps(summary, indent=2, sort_keys=True))
    for case in cases:
        if case["classification"] == MISMATCH:
            print(
                f"SILENT MISMATCH {case['program']}@{case['safe_point']}: "
                f"{case.get('differences')}",
                file=sys.stderr,
            )
        elif case["classification"] == INFRASTRUCTURE:
            print(
                f"INFRASTRUCTURE {case['program']}@{case['safe_point']}: "
                f"{case.get('detail')}",
                file=sys.stderr,
            )
    if mismatches or infrastructure:
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--source-python", default=sys.executable)
    root.add_argument("--target-python", default=sys.executable)
    root.add_argument("--checkpoints", type=int, default=5)
    root.add_argument("--program", action="append", default=[])
    root.add_argument("--workdir", default="/tmp/continuum-differential")
    root.add_argument(
        "--output",
        default=str(REPOSITORY / "compatibility" / "results" / "cross-python.json"),
    )
    root.add_argument("--timeout", type=float, default=300.0)
    root.add_argument("--keep-images", action="store_true")
    return root


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        return worker()
    return run_corpus(parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
