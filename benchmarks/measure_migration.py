#!/usr/bin/env python3
"""Measure the costs this release adds, on the current runtime.

Covers the eight figures the migration work is accountable for: compile
overhead, ordinary runtime slowdown against CPython, safe-point overhead,
freeze latency, image size, migration-plan generation, migration verification,
and resume latency.

Every number is a raw local measurement from one host. Nothing here is a
portable target, and nothing here was measured on macOS arm64 or Windows.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum import __version__, migration  # noqa: E402
from continuum.abi import EXECUTION_ABI_VERSION  # noqa: E402
from continuum.compiler import compile_source  # noqa: E402
from continuum.image import load_image, save_image  # noqa: E402
from continuum.vm import VirtualMachine  # noqa: E402

WORKLOAD = '''
class Tally:
    def __init__(self, seed):
        self.total = seed

    def add(self, value):
        self.total = self.total + value


def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, tally, bias, graph):
    index = 0
    while index < limit:
        tally.add(bias(index))
        graph["shared"].append(index)
        index += 1
    return tally.total


def middle(limit, tally, bias, graph):
    return leaf(limit, tally, bias, graph)


def outer(limit):
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    tally = Tally(7)
    bias = make_bias(3)
    return middle(limit, tally, bias, graph)


result = outer(ITERATIONS)
'''

REVISION_B = WORKLOAD.replace(
    "        index += 1", "        index += 1\n        pass"
)


def timed(action: Callable[[], Any], repetitions: int) -> dict[str, float]:
    samples = []
    for _ in range(repetitions):
        start = time.perf_counter()
        action()
        samples.append(time.perf_counter() - start)
    return {
        "median_seconds": statistics.median(samples),
        "min_seconds": min(samples),
        "samples": len(samples),
    }


def cpython_equivalent(iterations: int) -> Callable[[], Any]:
    namespace_source = WORKLOAD.replace("ITERATIONS", str(iterations))

    def run() -> None:
        namespace: dict[str, Any] = {}
        exec(compile(namespace_source, "<cpython>", "exec"), namespace)

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--freeze-safe-point", type=int, default=5_000)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    source = WORKLOAD.replace("ITERATIONS", str(args.iterations))
    results: dict[str, Any] = {
        "continuum_version": __version__,
        "execution_abi_version": EXECUTION_ABI_VERSION,
        "migration_plan_format": migration.PLAN_FORMAT_VERSION,
        "python_version": platform.python_version(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "iterations": args.iterations,
        "repetitions": args.repetitions,
    }

    # 1. Compile overhead: source text to Continuum IR.
    results["compile"] = timed(
        lambda: compile_source(source, "bench.py"), args.repetitions
    )

    ir = compile_source(source, "bench.py")

    # 2. Ordinary runtime, and the slowdown against CPython running the same
    # program. Safe points are on, because that is how Continuum always runs.
    def run_continuum() -> None:
        vm = VirtualMachine(ir, ["bench.py"], "bench.py")
        with contextlib.redirect_stdout(io.StringIO()):
            vm.run()

    results["continuum_run"] = timed(run_continuum, args.repetitions)
    results["cpython_run"] = timed(
        cpython_equivalent(args.iterations), args.repetitions
    )
    results["slowdown_vs_cpython"] = round(
        results["continuum_run"]["median_seconds"]
        / results["cpython_run"]["median_seconds"],
        1,
    )

    # 3. Safe-point overhead: the same run with a callback attached, which is
    # what a live session pays over a bare run.
    def run_with_callback() -> None:
        vm = VirtualMachine(ir, ["bench.py"], "bench.py")
        vm.safe_point_callback = lambda machine: None
        with contextlib.redirect_stdout(io.StringIO()):
            vm.run()

    results["continuum_run_with_safe_point_callback"] = timed(
        run_with_callback, args.repetitions
    )
    results["safe_point_callback_overhead_ratio"] = round(
        results["continuum_run_with_safe_point_callback"]["median_seconds"]
        / results["continuum_run"]["median_seconds"],
        3,
    )

    probe = VirtualMachine(ir, ["bench.py"], "bench.py")
    with contextlib.redirect_stdout(io.StringIO()):
        probe.run()
    results["safe_points_executed"] = probe.safe_points_executed
    results["instructions_executed"] = probe.instructions_executed

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        image = root / "bench.cont"

        def make_live() -> VirtualMachine:
            vm = VirtualMachine(ir, ["bench.py"], "bench.py")
            with contextlib.redirect_stdout(io.StringIO()):
                while vm.frames and vm.safe_points_executed < args.freeze_safe_point:
                    vm.step()
            return vm

        # 4. Freeze latency: encode, round-trip check, and durable write.
        # The VM is advanced to the checkpoint outside the timer; including
        # that stepping would report the workload's own cost as freeze cost.
        live = make_live()
        results["freeze"] = timed(
            lambda: save_image(image, live, source), args.repetitions
        )

        # 5. Image size.
        results["image_bytes"] = image.stat().st_size

        # 6. Migration-plan generation.
        candidate = root / "revision_b.py"
        candidate.write_text(
            REVISION_B.replace("ITERATIONS", str(args.iterations)), encoding="utf-8"
        )
        results["plan_upgrade"] = timed(
            lambda: migration.plan_upgrade(image, candidate), args.repetitions
        )

        plan = migration.plan_upgrade(image, candidate)
        plan_path = root / "bench.cup"
        new_source = candidate.read_text(encoding="utf-8")
        migration.write_plan(
            plan_path, plan, new_source, compile_source(new_source, "bench.py")
        )
        results["migration_plan_bytes"] = plan_path.stat().st_size

        # 7. Migration verification, which re-derives the plan independently.
        results["verify_upgrade"] = timed(
            lambda: migration.verify_plan(image, plan_path), args.repetitions
        )

        # 8. Resume latency: load, validate, decode the graph, rebuild frames.
        # Measured without running the restored program to completion.
        def restore_only() -> None:
            load_image(image).restore_vm()

        results["resume"] = timed(restore_only, args.repetitions)

        stored, _source, new_ir = migration.read_plan(plan_path)

        def restore_and_migrate() -> None:
            vm = load_image(image).restore_vm()
            migration.apply_plan(vm, stored, new_ir)

        results["resume_with_migration"] = timed(
            restore_and_migrate, args.repetitions
        )

    rendered = json.dumps(results, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
