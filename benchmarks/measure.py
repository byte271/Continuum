#!/usr/bin/env python3
"""Reproducible phase measurements; emits raw JSON and invents no targets."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path

from continuum import __version__
from continuum.codec import encode_graph
from continuum.compiler import compile_source
from continuum.image import _json_bytes, load_image, save_image
from continuum.vm import VirtualMachine


PROGRAM = """
def work(limit):
    values = []
    index = 0
    total = 0
    while index < limit:
        values.append(index)
        total += index * index
        index += 1
    return total

answer = work(__args__[1])
"""


def native_work(iterations: int) -> int:
    values = []
    index = 0
    answer = 0
    while index < iterations:
        values.append(index)
        answer += index * index
        index += 1
    return answer


def measure(callable_value, repetitions: int) -> tuple[list[float], object]:
    samples = []
    result = None
    for _ in range(repetitions):
        start = time.perf_counter()
        result = callable_value()
        samples.append(time.perf_counter() - start)
    return samples, result


def summary(samples: list[float]) -> dict[str, object]:
    return {
        "samples_seconds": samples,
        "median_seconds": statistics.median(samples),
        "minimum_seconds": min(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    compile_samples, ir = measure(
        lambda: compile_source(PROGRAM, "benchmark.py"),
        args.repetitions,
    )
    native_samples, native_answer = measure(
        lambda: native_work(args.iterations),
        args.repetitions,
    )

    def run_vm(callback=None):
        vm = VirtualMachine(
            ir,
            ["benchmark.py", args.iterations],
            "benchmark.py",
            safe_point_callback=callback,
        )
        vm.run()
        return vm

    vm_samples, full_vm = measure(
        run_vm,
        args.repetitions,
    )
    missing_request = (
        Path(tempfile.gettempdir())
        / f"continuum-benchmark-missing-request-{os.getpid()}"
    )
    polling_samples, polling_vm = measure(
        lambda: run_vm(lambda vm: missing_request.exists()),
        args.repetitions,
    )
    if (
        full_vm.globals["answer"] != native_answer
        or polling_vm.globals["answer"] != native_answer
    ):
        raise RuntimeError("benchmark control results differ")

    tracemalloc.start()
    memory_vm = run_vm()
    _, vm_peak_traced_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if memory_vm.globals["answer"] != native_answer:
        raise RuntimeError("memory measurement result differs")

    with tempfile.TemporaryDirectory() as temporary:
        image = Path(temporary) / "benchmark.cont"
        vm = VirtualMachine(
            ir, ["benchmark.py", args.iterations], "benchmark.py"
        )
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index", 0) < (
            args.iterations // 2
        ):
            vm.step()

        start = time.perf_counter()
        resource_records, _ = vm.resources.snapshot()
        resource_snapshot_seconds = time.perf_counter() - start
        start = time.perf_counter()
        heap = encode_graph(vm.state_root())
        graph_encode_seconds = time.perf_counter() - start
        start = time.perf_counter()
        _json_bytes(heap)
        heap_json_seconds = time.perf_counter() - start

        freeze_start = time.perf_counter()
        manifest = save_image(image, vm, PROGRAM)
        image_commit_seconds = time.perf_counter() - freeze_start
        load_start = time.perf_counter()
        loaded = load_image(image)
        image_load_validate_seconds = time.perf_counter() - load_start
        restore_start = time.perf_counter()
        restored = loaded.restore_vm()
        graph_resource_restore_seconds = time.perf_counter() - restore_start
        run_start = time.perf_counter()
        restored.run()
        remaining_seconds = time.perf_counter() - run_start
        if restored.globals["answer"] != native_answer:
            raise RuntimeError("resumed benchmark result differs")
        with zipfile.ZipFile(image, "r") as archive:
            expanded_bytes = sum(item.file_size for item in archive.infolist())

        native_median = statistics.median(native_samples)
        vm_median = statistics.median(vm_samples)
        result = {
            "environment": {
                "continuum_version": __version__,
                "machine": platform.machine(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
            },
            "workload": {
                "iterations": args.iterations,
                "repetitions": args.repetitions,
                "expected_answer": native_answer,
            },
            "compile": summary(compile_samples),
            "native_control": summary(native_samples),
            "vm_control_without_callback": summary(vm_samples),
            "vm_control_with_request_path_polling": summary(polling_samples),
            "runtime_slowdown_median": vm_median / native_median,
            "safe_point_callback_ratio_median": (
                statistics.median(polling_samples) / vm_median
            ),
            "checkpoint_phases_seconds": {
                "resource_snapshot": resource_snapshot_seconds,
                "graph_encode": graph_encode_seconds,
                "heap_json_encode": heap_json_seconds,
                "complete_image_commit": image_commit_seconds,
                "image_load_and_validate": image_load_validate_seconds,
                "resource_rebind_graph_decode_and_vm_restore": (
                    graph_resource_restore_seconds
                ),
                "remaining_execution": remaining_seconds,
            },
            "image": {
                "bytes": image.stat().st_size,
                "expanded_bytes": expanded_bytes,
                "compression_ratio": expanded_bytes / image.stat().st_size,
                "heap_objects": manifest["heap_objects"],
                "resource_records": len(resource_records),
                "instructions_before_freeze": vm.instructions_executed,
            },
            "memory": {
                "vm_peak_traced_bytes_separate_untimed_run": vm_peak_traced_bytes,
                "max_rss_platform_units": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss,
            },
        }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
