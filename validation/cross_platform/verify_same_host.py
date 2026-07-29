#!/usr/bin/env python3
"""Exercise the transfer package locally without satisfying cross-host proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    evidence_dir = args.input.resolve()
    source = json.loads(
        (evidence_dir / "source-evidence.json").read_text(encoding="utf-8")
    )
    image = evidence_dir / source["image"]
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    if image_hash != source["image_sha256"]:
        raise RuntimeError("image hash differs from source evidence")
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository),
        "CONTINUUM_HOME": str(evidence_dir / "same-host-resume-home"),
    }
    target = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "continuum",
            "resume",
            str(image),
            "--file-policy",
            "bundle",
        ],
        cwd=repository,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    target_pid = target.pid
    target_stdout, target_stderr = target.communicate(timeout=180)
    if target.returncode != 0:
        raise RuntimeError(target_stderr)
    control = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuum",
            "run",
            str(repository / "examples" / "cross_platform_proof.py"),
            str(evidence_dir / "control-input.txt"),
            str(source["iterations"]),
            source["nonce"],
        ],
        cwd=repository,
        env={
            **environment,
            "CONTINUUM_HOME": str(evidence_dir / "same-host-control-home"),
        },
        text=True,
        capture_output=True,
        timeout=180,
    )
    if control.returncode != 0:
        raise RuntimeError(control.stderr)
    combined = (
        (evidence_dir / "source-stdout.log").read_text(encoding="utf-8")
        + target_stdout
    )
    action_lines = [
        line
        for line in combined.splitlines()
        if line.startswith(f"ACTION {source['nonce']} ")
    ]
    final_hashes = re.findall(r"^FINAL ([0-9a-f]{64})$", combined, re.MULTILINE)
    control_hashes = re.findall(
        r"^FINAL ([0-9a-f]{64})$", control.stdout, re.MULTILINE
    )
    result = {
        "phase": "same-host-dry-run",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "source_pid_recorded": source["source_pid"],
        "source_process_exited": source["source_process_exited"],
        "target_pid": target_pid,
        "target_returncode": target.returncode,
        "numeric_pid_reused": target_pid == source["source_pid"],
        "new_target_process_started_after_recorded_source_exit": (
            source["source_process_exited"]
        ),
        "image_sha256_source": source["image_sha256"],
        "image_sha256_target": image_hash,
        "combined_output_lines": len(combined.splitlines()),
        "combined_output_matches_control": combined == control.stdout,
        "duplicate_irreversible_actions": len(action_lines) != len(set(action_lines)),
        "final_hash": final_hashes[0] if len(final_hashes) == 1 else None,
        "control_hash": control_hashes[0] if len(control_hashes) == 1 else None,
    }
    if not (
        result["new_target_process_started_after_recorded_source_exit"]
        and result["combined_output_matches_control"]
        and not result["duplicate_irreversible_actions"]
        and result["final_hash"] == result["control_hash"]
    ):
        raise RuntimeError(f"same-host validation failed: {result}")
    (evidence_dir / "same-host-target-stdout.log").write_text(
        target_stdout, encoding="utf-8"
    )
    (evidence_dir / "same-host-target-stderr.log").write_text(
        target_stderr, encoding="utf-8"
    )
    (evidence_dir / "same-host-control-stdout.log").write_text(
        control.stdout, encoding="utf-8"
    )
    (evidence_dir / "same-host-control-stderr.log").write_text(
        control.stderr, encoding="utf-8"
    )
    (evidence_dir / "same-host-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
