from __future__ import annotations

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from continuum.compiler import compile_source
from continuum.errors import ContinuumError, FrozenExecution
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine


ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = Path(__file__).resolve().parent / "programs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the CPython-versus-Continuum compatibility corpus."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "results" / "latest.json",
    )
    parser.add_argument("--program", action="append", default=[])
    return parser


def _features(source: str) -> list[str]:
    tree = ast.parse(source)
    ignored = {
        "Load",
        "Store",
        "Del",
        "Module",
        "Expr",
        "Constant",
        "Name",
    }
    return sorted(
        {
            type(node).__name__
            for node in ast.walk(tree)
            if type(node).__name__ not in ignored
        }
    )


def _runtime_features(source: str) -> list[str]:
    tree = ast.parse(source)
    features = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                features.add(f"stdlib:{alias.name}")
        elif isinstance(node, ast.FunctionDef):
            features.add("function_frames")
        elif isinstance(node, (ast.For, ast.While)):
            features.add("loop_safe_points")
        elif isinstance(node, ast.Try):
            features.add("exception_control_state")
        elif isinstance(node, (ast.List, ast.Dict, ast.Set)):
            features.add("mutable_object_graph")
    return sorted(features)


def _failure_category(exc: BaseException) -> str:
    message = str(exc)
    if "unsupported" in message.lower():
        return "unsupported_language_or_runtime"
    if "timed out" in message.lower():
        return "timeout"
    if isinstance(exc, ContinuumError):
        return "continuum_error"
    return "unexpected_error"


def _run_cpython(program: Path) -> tuple[bytes, bytes, float]:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, str(program)],
        cwd=program.parent,
        capture_output=True,
        timeout=10,
    )
    duration = time.perf_counter() - started
    if result.returncode != 0:
        raise RuntimeError(
            f"CPython exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout, result.stderr, duration


def _run_vm(
    ir: dict[str, Any], program: Path
) -> tuple[bytes, float, int]:
    output = io.StringIO()
    vm = VirtualMachine(ir, [str(program)], str(program))
    started = time.perf_counter()
    with redirect_stdout(output):
        vm.run()
    duration = time.perf_counter() - started
    return output.getvalue().encode(), duration, vm.safe_points_executed


def _freeze_source(
    ir: dict[str, Any],
    program: Path,
    source: str,
    image: Path,
    threshold: int,
) -> tuple[bytes, float]:
    output = io.StringIO()
    frozen = False

    def checkpoint(vm: VirtualMachine) -> None:
        nonlocal frozen
        if frozen or vm.safe_points_executed < threshold:
            return
        save_image(image, vm, source)
        frozen = True
        raise FrozenExecution

    vm = VirtualMachine(
        ir,
        [str(program)],
        str(program),
        safe_point_callback=checkpoint,
    )
    started = time.perf_counter()
    with redirect_stdout(output):
        try:
            vm.run()
        except FrozenExecution:
            pass
    duration = time.perf_counter() - started
    if not frozen:
        raise RuntimeError("program completed before a continuation was captured")
    return output.getvalue().encode(), duration


def _same_process_resume(
    ir: dict[str, Any],
    program: Path,
    source: str,
    threshold: int,
    root: Path,
) -> tuple[bytes, float]:
    image = root / "same-process.cont"
    before, source_seconds = _freeze_source(
        ir, program, source, image, threshold
    )
    output = io.StringIO()
    started = time.perf_counter()
    with redirect_stdout(output):
        load_image(image).restore_vm().run()
    target_seconds = time.perf_counter() - started
    return before + output.getvalue().encode(), source_seconds + target_seconds


def _new_process_resume(
    ir: dict[str, Any],
    program: Path,
    source: str,
    threshold: int,
    root: Path,
) -> tuple[bytes, float]:
    image = root / "new-process.cont"
    target_output = root / "new-process-target.log"
    before, source_seconds = _freeze_source(
        ir, program, source, image, threshold
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), environment.get("PYTHONPATH")))
    )
    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "compatibility.resume_case",
            str(image),
            str(target_output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    target_seconds = time.perf_counter() - started
    if result.returncode != 0:
        raise RuntimeError(
            f"target process exited {result.returncode}: {result.stderr.strip()}"
        )
    return before + target_output.read_bytes(), source_seconds + target_seconds


def run_case(program: Path) -> dict[str, Any]:
    source = program.read_text(encoding="utf-8")
    result: dict[str, Any] = {
        "program": program.stem,
        "path": str(program.relative_to(ROOT)),
        "source": "Continuum project compatibility corpus",
        "license": "MIT",
        "lines_of_code": sum(
            1
            for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        "syntax_features": _features(source),
        "runtime_features": _runtime_features(source),
        "compile": "not_run",
        "run": "not_run",
        "matches_cpython": False,
        "freezes": "not_run",
        "same_process": "not_run",
        "new_process": "not_run",
        "cross_platform": "not_run",
        "cp_python_output_match": False,
        "failure": None,
        "performance_seconds": {},
    }
    try:
        cpython_stdout, cpython_stderr, cpython_seconds = _run_cpython(program)
        result["performance_seconds"]["cpython"] = cpython_seconds
        result["cpython_stderr"] = cpython_stderr.decode("utf-8", "replace")
    except BaseException as exc:
        result["failure"] = {
            "stage": "cpython",
            "category": _failure_category(exc),
            "message": str(exc),
        }
        return result

    try:
        compile_started = time.perf_counter()
        ir = compile_source(source, str(program))
        result["performance_seconds"]["compile"] = (
            time.perf_counter() - compile_started
        )
        result["compile"] = "passed"
    except BaseException as exc:
        result["compile"] = "failed"
        result["failure"] = {
            "stage": "compile",
            "category": _failure_category(exc),
            "message": str(exc),
        }
        return result

    try:
        vm_stdout, vm_seconds, safe_points = _run_vm(ir, program)
        result["performance_seconds"]["continuum_uninterrupted"] = vm_seconds
        result["safe_points"] = safe_points
        result["run"] = "passed"
        result["matches_cpython"] = vm_stdout == cpython_stdout
    except BaseException as exc:
        result["run"] = "failed"
        result["failure"] = {
            "stage": "run",
            "category": _failure_category(exc),
            "message": str(exc),
        }
        return result

    if not result["matches_cpython"]:
        result["failure"] = {
            "stage": "run",
            "category": "output_mismatch",
            "message": "uninterrupted Continuum stdout differs from CPython",
        }
        return result

    threshold = max(1, safe_points // 2)
    try:
        with tempfile.TemporaryDirectory() as temporary:
            resumed, duration = _same_process_resume(
                ir, program, source, threshold, Path(temporary)
            )
        result["performance_seconds"]["same_process_resume"] = duration
        result["freezes"] = "passed"
        result["same_process"] = (
            "passed" if resumed == cpython_stdout else "failed"
        )
    except BaseException as exc:
        result["freezes"] = "failed"
        result["same_process"] = "failed"
        result["failure"] = {
            "stage": "same_process",
            "category": _failure_category(exc),
            "message": str(exc),
        }
        return result
    if result["same_process"] != "passed":
        result["failure"] = {
            "stage": "same_process",
            "category": "output_mismatch",
            "message": "same-process resumed stdout differs from CPython",
        }
        return result

    try:
        with tempfile.TemporaryDirectory() as temporary:
            resumed, duration = _new_process_resume(
                ir, program, source, threshold, Path(temporary)
            )
        result["performance_seconds"]["new_process_resume"] = duration
        result["new_process"] = (
            "passed" if resumed == cpython_stdout else "failed"
        )
        result["cp_python_output_match"] = resumed == cpython_stdout
    except BaseException as exc:
        result["new_process"] = "failed"
        result["failure"] = {
            "stage": "new_process",
            "category": _failure_category(exc),
            "message": str(exc),
        }
        return result
    if result["new_process"] != "passed":
        result["failure"] = {
            "stage": "new_process",
            "category": "output_mismatch",
            "message": "new-process resumed stdout differs from CPython",
        }
    return result


def _write_report(path: Path, results: list[dict[str, Any]]) -> None:
    passed = [
        item
        for item in results
        if item["new_process"] == "passed"
        and item["cp_python_output_match"]
    ]
    compile_failures: dict[str, int] = {}
    for item in results:
        failure = item.get("failure")
        if not failure or failure["stage"] != "compile":
            continue
        message = failure["message"]
        if "unsupported syntax " in message:
            feature = message.split("unsupported syntax ", 1)[1]
        else:
            feature = message.rsplit(": ", 1)[-1]
        compile_failures[feature] = compile_failures.get(feature, 0) + 1

    lines = [
        "# Compatibility corpus report",
        "",
        f"- Programs: {len(results)}",
        f"- Compile passed: {sum(item['compile'] == 'passed' for item in results)}",
        f"- Uninterrupted output matched CPython: {sum(item['matches_cpython'] for item in results)}",
        f"- Same-process resume passed: {sum(item['same_process'] == 'passed' for item in results)}",
        f"- New-process resume matched CPython: {len(passed)}",
        f"- Compatibility rate: {len(passed) / len(results) * 100:.1f}%",
        "- Cross-platform corpus results: not run",
        "- Timing fields are raw wall-clock diagnostics; CPython includes "
        "process startup, so this report does not calculate a slowdown ratio",
    ]
    lines.extend(
        [
            "",
            "## Compile failures",
            "",
            "| Diagnostic | Count |",
            "| --- | ---: |",
        ]
    )
    for feature, count in sorted(
        compile_failures.items(), key=lambda item: (-item[1], item[0])
    ):
        lines.append(f"| `{feature}` | {count} |")
    lines.extend(
        [
            "",
            "## Programs",
            "",
            "| Program | Compile | Run | Same process | New process | Failure |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in results:
        failure = item["failure"]
        failure_text = "" if failure is None else failure["category"]
        lines.append(
            f"| `{item['program']}` | {item['compile']} | {item['run']} | "
            f"{item['same_process']} | {item['new_process']} | "
            f"{failure_text} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = _parser().parse_args()
    selected = set(args.program)
    programs = sorted(PROGRAMS.glob("*.py"))
    if selected:
        programs = [program for program in programs if program.stem in selected]
    if not programs:
        raise SystemExit("no compatibility programs selected")
    results = []
    for index, program in enumerate(programs, 1):
        print(f"[{index}/{len(programs)}] {program.stem}", flush=True)
        results.append(run_case(program))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "format_version": 1,
        "python_version": sys.version.split()[0],
        "program_count": len(results),
        "results": results,
    }
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = output.with_suffix(".md")
    _write_report(report, results)
    passed = sum(
        item["new_process"] == "passed"
        and item["cp_python_output_match"]
        for item in results
    )
    print(f"New-process compatibility: {passed}/{len(results)}")
    print(f"Results: {output}")
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
