#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path


def run(command: list[str], cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rendered = "$ " + " ".join(command) + "\n" + completed.stdout
    if not rendered.endswith("\n"):
        rendered += "\n"
    rendered += f"[exit {completed.returncode}]\n\n"
    return completed.returncode, rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    commands = [
        ["uname", "-a"],
        ["uname", "-m"],
        [sys.executable, "--version"],
        [sys.executable, "-m", "continuum", "--version"],
        ["git", "status", "--short"],
        [sys.executable, "-m", "compileall", "-q", "continuum", "tests",
         "benchmarks", "validation", "examples"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    ]
    sections = [
        "Continuum final verification\n",
        f"UTC: {datetime.datetime.now(datetime.UTC).isoformat()}\n",
        f"cwd: {repository}\n",
        f"PYTHONHASHSEED: {os.environ.get('PYTHONHASHSEED', 'not set')}\n\n",
    ]
    failed = False
    for command in commands:
        returncode, output = run(command, repository)
        sections.append(output)
        if command[0:2] == ["git", "status"]:
            continue
        failed = failed or returncode != 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(sections), encoding="utf-8")
    print(f"wrote {args.output}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
