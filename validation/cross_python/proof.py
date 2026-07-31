#!/usr/bin/env python3
"""Two-job proof for one unchanged image crossing OS, ISA, and Python version."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum.compiler import compile_source  # noqa: E402
from continuum.portable_image import (  # noqa: E402
    EXECUTION_ABI_VERSION,
    SUPPORTED_PYTHON_VERSIONS,
    load_portable_image,
    save_portable_image,
    verify_portable_image,
)
from continuum.vm import VirtualMachine  # noqa: E402

SOURCE_PYTHON = "3.12.13"
TARGET_PYTHON = "3.13.14"
LIMIT = 40
CHECKPOINT_INDEX = 20

PROGRAM = r'''
class Accumulator:
    def __init__(self, seed):
        self.total = seed

    def add(self, value):
        self.total = self.total + value


def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, accumulator, bias, graph):
    index = 0
    while index < limit:
        value = bias(index)
        accumulator.add(value)
        graph["shared"].append(value)
        print(f"ACTION {index} {accumulator.total}")
        index += 1
    return accumulator.total


def middle(limit, accumulator, bias, graph):
    return leaf(limit, accumulator, bias, graph)


def outer(limit):
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    accumulator = Accumulator(7)
    bias = make_bias(3)
    answer = middle(limit, accumulator, bias, graph)
    print(f"FINAL {answer} {len(shared)}")
    return answer


result = outer(40)
'''


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _frame_names(vm: VirtualMachine) -> list[str]:
    return [vm.ir["functions"][frame.function_id]["name"] for frame in vm.frames]


def _close_resources(vm: VirtualMachine) -> None:
    for resource in vm.resources.files.values():
        resource.close()


def _assert_source_host() -> None:
    if platform.system() != "Linux":
        raise RuntimeError(f"source must be Linux, got {platform.system()}")
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError(f"source must be x86_64, got {platform.machine()}")
    if platform.python_version() != SOURCE_PYTHON:
        raise RuntimeError(
            f"source requires Python {SOURCE_PYTHON}, got {platform.python_version()}"
        )


def _assert_target_host() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError(f"target must be macOS, got {platform.system()}")
    if platform.machine().lower() != "arm64":
        raise RuntimeError(f"target must be native arm64, got {platform.machine()}")
    if platform.python_version() != TARGET_PYTHON:
        raise RuntimeError(
            f"target requires Python {TARGET_PYTHON}, got {platform.python_version()}"
        )


def create_source_evidence(output: Path) -> None:
    _assert_source_host()
    output.mkdir(parents=True, exist_ok=False)
    program_path = output / "program.py"
    image_path = output / "linux-py312.cont"
    source_output_path = output / "source-output.log"
    program_path.write_text(PROGRAM, encoding="utf-8")

    vm = VirtualMachine(
        compile_source(PROGRAM, "cross_python_program.py"),
        ["cross_python_program.py"],
        "cross_python_program.py",
    )
    source_stream = io.StringIO()
    steps = 0
    with contextlib.redirect_stdout(source_stream):
        while True:
            if not vm.frames:
                raise RuntimeError("workload completed before the checkpoint")
            frame = vm.frames[-1]
            function = vm.ir["functions"][frame.function_id]
            instruction = function["code"][frame.pc]
            if (
                function["name"] == "leaf"
                and frame.locals.get("index") == CHECKPOINT_INDEX
                and instruction["op"] == "SAFEPOINT"
            ):
                vm.step()
                break
            vm.step()
            steps += 1
            if steps > 1_000_000:
                raise RuntimeError("checkpoint condition was never reached")

    frame_names = _frame_names(vm)
    if frame_names != ["__module__", "outer", "middle", "leaf"]:
        raise RuntimeError(f"unexpected live frame chain: {frame_names}")
    leaf_frame = vm.frames[-1]
    graph = leaf_frame.locals["graph"]
    if graph["left"] is not graph["right"] or graph["self"] is not graph:
        raise RuntimeError("source graph identity invariant failed")

    source_output = source_stream.getvalue().encode("utf-8")
    source_output_path.write_bytes(source_output)
    save_portable_image(
        image_path,
        vm,
        PROGRAM,
        source_os="Linux",
        source_architecture="x86_64",
    )
    _close_resources(vm)

    actions = [
        int(match.group(1))
        for match in re.finditer(rb"^ACTION (\d+) ", source_output, re.MULTILINE)
    ]
    expected = list(range(CHECKPOINT_INDEX))
    if actions != expected:
        raise RuntimeError(
            f"source emitted unexpected completed actions: {actions} != {expected}"
        )

    evidence = {
        "execution_abi": EXECUTION_ABI_VERSION,
        "supported_python_versions": list(SUPPORTED_PYTHON_VERSIONS),
        "source": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "pid": os.getpid(),
        },
        "image": {
            "name": image_path.name,
            "sha256": _sha256_file(image_path),
            "bytes": image_path.stat().st_size,
        },
        "checkpoint": {
            "completed_actions": actions,
            "next_action": CHECKPOINT_INDEX,
            "frame_names": frame_names,
            "frames": len(vm.frames),
            "shared_reference_preserved": graph["left"] is graph["right"],
            "cycle_preserved": graph["self"] is graph,
            "instructions_executed": vm.instructions_executed,
            "safe_points_executed": vm.safe_points_executed,
        },
    }
    _write_json(output / "source-evidence.json", evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))


def resume_on_target(input_dir: Path, output: Path) -> None:
    _assert_target_host()
    source_evidence = json.loads(
        (input_dir / "source-evidence.json").read_text(encoding="utf-8")
    )
    image_path = input_dir / source_evidence["image"]["name"]
    source_output = (input_dir / "source-output.log").read_bytes()
    source_hash = source_evidence["image"]["sha256"]
    transferred_hash = _sha256_file(image_path)
    if transferred_hash != source_hash:
        raise RuntimeError(
            f"image changed during transfer: {transferred_hash} != {source_hash}"
        )
    if source_evidence["source"]["python_version"] != SOURCE_PYTHON:
        raise RuntimeError("source evidence does not name the required Python")

    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(image_path, output / image_path.name)
    shutil.copy2(input_dir / "source-evidence.json", output / "source-evidence.json")
    shutil.copy2(input_dir / "source-output.log", output / "source-output.log")
    shutil.copy2(input_dir / "program.py", output / "program.py")

    verification = verify_portable_image(image_path)
    loaded = load_portable_image(image_path)
    vm = loaded.restore_vm()
    frame_names = _frame_names(vm)
    leaf_frame = vm.frames[-1]
    graph = leaf_frame.locals["graph"]
    shared_reference = graph["left"] is graph["right"]
    cycle = graph["self"] is graph
    if frame_names != ["__module__", "outer", "middle", "leaf"]:
        raise RuntimeError(f"target restored unexpected frames: {frame_names}")
    if not shared_reference or not cycle:
        raise RuntimeError("target graph identity invariant failed")

    target_stream = io.StringIO()
    with contextlib.redirect_stdout(target_stream):
        resumed_result = vm.run()
    _close_resources(vm)
    target_output = target_stream.getvalue().encode("utf-8")
    (output / "target-output.log").write_bytes(target_output)

    control_vm = VirtualMachine(
        compile_source(PROGRAM, "cross_python_program.py"),
        ["cross_python_program.py"],
        "cross_python_program.py",
    )
    control_stream = io.StringIO()
    with contextlib.redirect_stdout(control_stream):
        control_result = control_vm.run()
    _close_resources(control_vm)
    control_output = control_stream.getvalue().encode("utf-8")
    (output / "control-output.log").write_bytes(control_output)

    combined = source_output + target_output
    (output / "combined-output.log").write_bytes(combined)
    actions = [
        int(match.group(1))
        for match in re.finditer(rb"^ACTION (\d+) ", combined, re.MULTILINE)
    ]
    expected_actions = list(range(LIMIT))
    if actions != expected_actions:
        raise RuntimeError(
            f"actions repeated or disappeared: {actions} != {expected_actions}"
        )
    if combined != control_output:
        raise RuntimeError("source plus target output differs from uninterrupted control")
    if resumed_result != control_result:
        raise RuntimeError("resumed result differs from uninterrupted control")

    report = {
        "proof": "cross-os-cross-isa-cross-python-portable-execution-image",
        "execution_abi": EXECUTION_ABI_VERSION,
        "source": source_evidence["source"],
        "target": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "pid": os.getpid(),
        },
        "image": {
            "sha256_at_source": source_hash,
            "sha256_after_transfer": transferred_hash,
            "byte_identical": source_hash == transferred_hash,
        },
        "restoration": {
            "frame_names": frame_names,
            "frames": len(frame_names),
            "shared_reference_preserved": shared_reference,
            "cycle_preserved": cycle,
            "completed_actions_repeated": 0,
            "all_actions_exactly_once": actions == expected_actions,
            "combined_output_matches_control": combined == control_output,
            "resumed_result_matches_control": resumed_result == control_result,
            "source_actions": CHECKPOINT_INDEX,
            "target_actions": LIMIT - CHECKPOINT_INDEX,
        },
        "verification": verification,
        "hashes": {
            "combined_output_sha256": hashlib.sha256(combined).hexdigest(),
            "control_output_sha256": hashlib.sha256(control_output).hexdigest(),
        },
    }
    _write_json(output / "final-report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source")
    source.add_argument("--output", type=Path, required=True)
    target = subparsers.add_parser("target")
    target.add_argument("--input", type=Path, required=True)
    target.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "source":
        create_source_evidence(args.output.resolve())
    else:
        resume_on_target(args.input.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
