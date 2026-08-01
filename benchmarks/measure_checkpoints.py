#!/usr/bin/env python3
"""Rolling checkpoint measurements; emits raw JSON and invents no targets.

Reports scheduling overhead, pause time, durable commit latency, image size,
write amplification, coalescing, and recovery scan time, separately and per
workload. Numbers are only ever valid for the host that produced them: the
report embeds the platform, interpreter, and state size so a Linux figure is
never mistaken for a Windows or macOS one.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from continuum import __version__
from continuum.checkpoint import CheckpointScheduler, CheckpointStore
from continuum.compiler import compile_source
from continuum.vm import VirtualMachine

# Small, flat state: the cheapest realistic checkpoint.
SMALL_STATE = """
def work(limit):
    total = 0
    index = 0
    while index < limit:
        total = total + index
        index += 1
    return total


answer = work(%d)
"""

# A larger live object graph, including sharing and a cycle, so the heap codec
# does real work on every capture.
LARGE_GRAPH = """
def build(width):
    rows = []
    index = 0
    while index < width:
        row = {"index": index, "values": [index, index + 1, index + 2]}
        rows.append(row)
        index += 1
    graph = {"rows": rows, "left": rows, "right": rows}
    graph["self"] = graph
    return graph


def work(limit, width):
    graph = build(width)
    total = 0
    index = 0
    while index < limit:
        total = total + len(graph["rows"])
        index += 1
    return total


answer = work(%d, %d)
"""

WORKLOADS = {
    "small-state": lambda iterations: SMALL_STATE % iterations,
    "large-graph": lambda iterations: LARGE_GRAPH % (iterations, 400),
}


def _run_workload(source: str, directory: Path, interval: float) -> dict:
    store = CheckpointStore(directory)
    scheduler = CheckpointScheduler(
        store, source, lineage_id="bench", interval_seconds=interval
    )
    vm = VirtualMachine(
        compile_source(source, "bench.py"),
        ["bench.py"],
        "bench.py",
        safe_point_callback=scheduler.on_safe_point,
    )
    started = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        vm.run()
    wall = time.perf_counter() - started
    pauses = [item.pause_seconds for item in scheduler.history]
    commits = [item.commit_seconds for item in scheduler.history]
    sizes = [item.image_bytes for item in scheduler.history]
    return {
        "requested_interval_seconds": interval,
        "wall_seconds": wall,
        "checkpoints_committed": len(scheduler.history),
        "coalesced_ticks": scheduler.status.coalesced_ticks,
        "failures": scheduler.status.failures,
        "pause_seconds": _summary(pauses),
        "commit_seconds": _summary(commits),
        "image_bytes": _summary(sizes),
        "bytes_written_total": sum(sizes),
        # Every checkpoint rewrites the whole image, so amplification is the
        # total written divided by what one image costs. This is the cost the
        # first implementation deliberately accepts; see PERFORMANCE.md.
        "write_amplification": (
            sum(sizes) / sizes[-1] if sizes else None
        ),
        "safe_points_executed": vm.safe_points_executed,
        "instructions_executed": vm.instructions_executed,
    }


def _baseline_overhead(source: str) -> dict:
    """Safe-point cost with checkpointing enabled but never due."""

    with tempfile.TemporaryDirectory() as directory:
        store = CheckpointStore(Path(directory) / "cp")
        # An interval far longer than the run: the scheduler is consulted at
        # every safe point but never commits.
        scheduler = CheckpointScheduler(
            store, source, lineage_id="bench", interval_seconds=3600.0
        )
        vm = VirtualMachine(
            compile_source(source, "bench.py"), ["bench.py"], "bench.py",
            safe_point_callback=scheduler.on_safe_point,
        )
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            vm.run()
        scheduled = time.perf_counter() - started
        assert scheduler.status.commits == 0, "baseline must not checkpoint"

    plain = VirtualMachine(
        compile_source(source, "bench.py"), ["bench.py"], "bench.py"
    )
    started = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        plain.run()
    bare = time.perf_counter() - started
    return {
        "with_scheduler_seconds": scheduled,
        "without_callback_seconds": bare,
        "overhead_seconds": scheduled - bare,
        "safe_points_executed": plain.safe_points_executed,
        "overhead_microseconds_per_safe_point": (
            (scheduled - bare) / plain.safe_points_executed * 1_000_000
            if plain.safe_points_executed
            else None
        ),
    }


def _recovery_scan(directory: Path) -> dict:
    started = time.perf_counter()
    store = CheckpointStore(directory)
    result = store.recover()
    elapsed = time.perf_counter() - started
    return {
        "scan_and_validate_seconds": elapsed,
        "selected_generation": (
            result.selected.generation if result.selected else None
        ),
        "slots_validated": len(result.candidates),
    }


def _summary(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "max": ordered[-1],
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument(
        "--intervals", default="100ms,1s,5s",
        help="comma-separated requested intervals to measure",
    )
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()

    from continuum.checkpoint import parse_interval

    intervals = [parse_interval(item) for item in arguments.intervals.split(",")]
    report = {
        "continuum_version": __version__,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "iterations": arguments.iterations,
        # Stated explicitly so no reader can generalise these numbers to
        # another platform or another state size.
        "measurement_scope": (
            "single host; figures are not portable across operating systems, "
            "filesystems, or state sizes"
        ),
        "workloads": {},
    }
    for name, builder in WORKLOADS.items():
        source = builder(arguments.iterations)
        entry = {"baseline": _baseline_overhead(source), "intervals": {}}
        for interval in intervals:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cp"
                measured = _run_workload(source, path, interval)
                measured["recovery"] = _recovery_scan(path)
                entry["intervals"][f"{interval}s"] = measured
        report["workloads"][name] = entry

    text = json.dumps(report, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
