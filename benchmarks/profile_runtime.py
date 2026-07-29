#!/usr/bin/env python3
from __future__ import annotations

import argparse
import cProfile
import io
import pstats
from pathlib import Path

from continuum.compiler import compile_source
from continuum.vm import VirtualMachine

from benchmarks.measure import PROGRAM, native_work


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    ir = compile_source(PROGRAM, "profile.py")
    vm = VirtualMachine(ir, ["profile.py", args.iterations], "profile.py")
    profile = cProfile.Profile()
    profile.enable()
    vm.run()
    profile.disable()
    if vm.globals["answer"] != native_work(args.iterations):
        raise RuntimeError("profile workload result differs from native control")
    args.profile.parent.mkdir(parents=True, exist_ok=True)
    profile.dump_stats(args.profile)
    output = io.StringIO()
    stats = pstats.Stats(profile, stream=output).strip_dirs().sort_stats("tottime")
    stats.print_stats(40)
    output.write("\n")
    pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(
        40
    )
    args.report.write_text(output.getvalue(), encoding="utf-8")
    print(output.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
