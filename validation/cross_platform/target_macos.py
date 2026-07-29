#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from common import (
    EXPECTED_PYTHON,
    checked_stdout,
    git_identity,
    render_raw_commands,
    run_full_tests,
    sha256_file,
    utc_now,
    verify_repository_identity,
    write_failure,
    write_json,
)


FINAL_PATTERN = re.compile(rb"^FINAL ([0-9a-f]{64})$", re.MULTILINE)


def perform(evidence_dir: Path) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    if platform.system() != "Darwin":
        raise RuntimeError("target operating system is not Darwin")
    if platform.machine().lower() != "arm64":
        raise RuntimeError("target Python process is not running as arm64")
    if platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"target requires Python {EXPECTED_PYTHON}; "
            f"current is {platform.python_version()}"
        )

    source_evidence = json.loads(
        (evidence_dir / "source-evidence.json").read_text(encoding="utf-8")
    )
    if not source_evidence.get("qualified_real_linux_x86_64"):
        raise RuntimeError("source evidence is not qualified real Linux x86_64")
    if source_evidence.get("rehearsal"):
        raise RuntimeError("rehearsal source evidence cannot be used on target")
    if not (
        source_evidence.get("source_process_exited")
        and source_evidence.get("source_process_reaped")
    ):
        raise RuntimeError("source exit and reap are not proven")
    if not source_evidence.get("deleted_original_input"):
        raise RuntimeError("original bundled input was not deleted on source")

    identity = verify_repository_identity(repository, evidence_dir)
    if identity["git_commit"] != source_evidence["git_commit"]:
        raise RuntimeError("source evidence Git commit is inconsistent")
    if (
        identity["source_tree_manifest_sha256"]
        != source_evidence["source_tree_manifest_sha256"]
    ):
        raise RuntimeError("source-tree manifest identity is inconsistent")

    image = evidence_dir / source_evidence["image"]
    if not image.is_file():
        raise RuntimeError("Linux image is missing")
    image.chmod(0o444)
    before_hash = sha256_file(image)
    (evidence_dir / "image-target-before.sha256").write_text(
        f"{before_hash}  {image.name}\n", encoding="utf-8"
    )
    if before_hash != source_evidence["image_sha256"]:
        raise RuntimeError("image SHA-256 differs from the Linux source record")

    environment = {
        **os.environ,
        "PYTHONPATH": str(repository),
        "CONTINUUM_HOME": str(evidence_dir / "target-continuum-home"),
    }
    python_executable = checked_stdout(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.executable)",
        ],
        repository,
    )
    python_binary = str(Path(python_executable).resolve())
    raw_environment, environment_results = render_raw_commands(
        [
            ["sw_vers"],
            ["uname", "-a"],
            ["uname", "-m"],
            ["arch"],
            ["python3", "--version"],
            [
                "python3",
                "-c",
                (
                    "import platform, sys; "
                    "print(platform.system()); "
                    "print(platform.machine()); "
                    "print(sys.executable)"
                ),
            ],
            ["file", python_binary],
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            ["sysctl", "-in", "sysctl.proc_translated"],
            ["python3", "-m", "continuum", "--version"],
            ["git", "rev-parse", "HEAD"],
            ["git", "status", "--porcelain=v1"],
            ["shasum", "-a", "256", str(image)],
            [
                "shasum",
                "-a",
                "256",
                str(evidence_dir / identity["repository_archive"]),
            ],
        ],
        repository,
        environment=environment,
    )
    (evidence_dir / "macos-environment.txt").write_text(
        raw_environment, encoding="utf-8"
    )
    # sysctl.proc_translated commonly returns exit 1 when the key is absent on
    # a native process. Every other environment command must succeed.
    for index, result in enumerate(environment_results):
        if index == 8 and result.returncode in {0, 1}:
            continue
        if result.returncode != 0:
            raise RuntimeError(
                f"macOS environment command {index} failed; "
                "see macos-environment.txt"
            )
    if environment_results[2].stdout.strip() != "arm64":
        raise RuntimeError("uname -m is not arm64")
    if environment_results[3].stdout.strip() != "arm64":
        raise RuntimeError("arch is not arm64")
    if environment_results[4].stdout.strip() != f"Python {EXPECTED_PYTHON}":
        raise RuntimeError("python3 is not exactly Python 3.12.13")
    python_identity_lines = environment_results[5].stdout.splitlines()
    if python_identity_lines[:2] != ["Darwin", "arm64"]:
        raise RuntimeError("python3 does not report native Darwin arm64")
    if "arm64" not in environment_results[6].stdout.lower():
        raise RuntimeError("Python executable is not arm64-capable")
    if "apple" not in environment_results[7].stdout.lower():
        raise RuntimeError("CPU brand does not identify Apple Silicon")
    if (
        environment_results[8].returncode == 0
        and environment_results[8].stdout.strip() == "1"
    ):
        raise RuntimeError("Python is running under Rosetta translation")
    if environment_results[11].stdout:
        raise RuntimeError("Git working tree is not clean on macOS")

    run_full_tests(
        repository,
        evidence_dir / "full-test-macos.txt",
        {
            **environment,
            "CONTINUUM_HOME": str(evidence_dir / "macos-test-home"),
        },
    )
    final_commit, _ = git_identity(repository)
    if final_commit != source_evidence["git_commit"]:
        raise RuntimeError("Git commit changed before target resume")

    target_started_at = utc_now()
    target_started_ns = time.time_ns()
    if target_started_ns <= int(source_evidence["source_exited_unix_ns"]):
        raise RuntimeError("target was created before the recorded source exit")
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    target_pid = target.pid
    target_stdout, target_stderr = target.communicate(timeout=300)
    target_exited_at = utc_now()
    target_exited_ns = time.time_ns()
    (evidence_dir / "target-stdout.log").write_bytes(target_stdout)
    (evidence_dir / "target-stderr.log").write_bytes(target_stderr)
    after_hash = sha256_file(image)
    (evidence_dir / "image-target-after.sha256").write_text(
        f"{after_hash}  {image.name}\n", encoding="utf-8"
    )
    if after_hash != before_hash:
        raise RuntimeError("image changed during target resume")
    if target.returncode != 0:
        raise RuntimeError(
            "target resume failed: "
            + target_stderr.decode("utf-8", errors="replace")
        )

    control = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuum",
            "run",
            str(repository / "examples" / "cross_platform_proof.py"),
            str(evidence_dir / "control-input.txt"),
            str(source_evidence["iterations"]),
            source_evidence["nonce"],
        ],
        cwd=repository,
        env={
            **environment,
            "CONTINUUM_HOME": str(evidence_dir / "control-continuum-home"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    (evidence_dir / "control-stdout.log").write_bytes(control.stdout)
    (evidence_dir / "control-stderr.log").write_bytes(control.stderr)
    if control.returncode != 0:
        raise RuntimeError(
            "control run failed: "
            + control.stderr.decode("utf-8", errors="replace")
        )

    source_stdout = (evidence_dir / "source-stdout.log").read_bytes()
    combined = source_stdout + target_stdout
    (evidence_dir / "combined-output.log").write_bytes(combined)
    action_prefix = f"ACTION {source_evidence['nonce']} ".encode("utf-8")
    action_lines = [
        line for line in combined.splitlines() if line.startswith(action_prefix)
    ]
    entry_line = f"ACTION {source_evidence['nonce']} ENTRY".encode("utf-8")
    iteration_prefix = (
        f"ACTION {source_evidence['nonce']} ITER ".encode("utf-8")
    )
    iteration_lines = [
        line for line in action_lines if line.startswith(iteration_prefix)
    ]
    final_hashes = FINAL_PATTERN.findall(combined)
    control_hashes = FINAL_PATTERN.findall(control.stdout)
    compatibility_accepted = b"Compatibility accepted:" in target_stderr
    comparison = {
        "condition_01_real_linux_x86_64_source": True,
        "condition_02_source_exited_before_target_created": (
            bool(source_evidence["source_process_exited"])
            and bool(source_evidence["source_process_reaped"])
            and target_started_ns > int(source_evidence["source_exited_unix_ns"])
        ),
        "condition_03_native_real_macos_arm64_target": True,
        "condition_04_same_git_commit": final_commit
        == source_evidence["git_commit"],
        "condition_05_python_3_12_13_both": (
            source_evidence.get("python_version", EXPECTED_PYTHON)
            == EXPECTED_PYTHON
            and platform.python_version() == EXPECTED_PYTHON
        ),
        "condition_06_exact_same_image_bytes": (
            source_evidence["image_sha256"] == before_hash == after_hash
        ),
        "condition_07_image_sha256_matches_both": (
            source_evidence["image_sha256"] == before_hash == after_hash
        ),
        "condition_08_resume_did_not_execute_or_recompile_source": (
            target.returncode == 0 and entry_line not in target_stdout
        ),
        "condition_09_original_input_absent": (
            bool(source_evidence["deleted_original_input"])
            and not (evidence_dir / "proof-input.txt").exists()
        ),
        "condition_10_bundled_file_restored": target.returncode == 0,
        "condition_11_entry_actions_not_repeated": action_lines.count(entry_line)
        == 1,
        "condition_12_function_prologues_not_repeated": (
            len(iteration_lines) == int(source_evidence["iterations"])
            and len(iteration_lines) == len(set(iteration_lines))
        ),
        "condition_13_completed_loop_actions_not_repeated": (
            len(iteration_lines) == len(set(iteration_lines))
        ),
        "condition_14_combined_output_matches_control": combined
        == control.stdout,
        "condition_15_final_hash_matches_control": (
            len(final_hashes) == 1
            and len(control_hashes) == 1
            and final_hashes == control_hashes
        ),
        "condition_16_no_native_linux_payload_required": (
            source_evidence["native_payload_required"] is False
        ),
        "condition_17_no_compatibility_bypass": compatibility_accepted,
        "condition_18_all_raw_evidence_retained": all(
            (evidence_dir / name).is_file()
            for name in (
                "git-commit.txt",
                "source-tree.sha256",
                "repository.sha256",
                "linux-environment.txt",
                "macos-environment.txt",
                "source-evidence.json",
                "linux-x86_64.cont",
                "image-source.sha256",
                "image-target-before.sha256",
                "image-target-after.sha256",
                "source-stdout.log",
                "source-stderr.log",
                "target-stdout.log",
                "target-stderr.log",
                "control-stdout.log",
                "control-stderr.log",
                "combined-output.log",
                "full-test-linux.txt",
                "full-test-macos.txt",
            )
        ),
        "image_sha256_source": source_evidence["image_sha256"],
        "image_sha256_target_before": before_hash,
        "image_sha256_target_after": after_hash,
        "source_output_lines": len(source_stdout.splitlines()),
        "target_output_lines": len(target_stdout.splitlines()),
        "combined_output_lines": len(combined.splitlines()),
        "control_output_lines": len(control.stdout.splitlines()),
        "irreversible_action_lines": len(action_lines),
        "duplicate_irreversible_actions": len(action_lines)
        != len(set(action_lines)),
        "final_hash": (
            final_hashes[0].decode("ascii") if len(final_hashes) == 1 else None
        ),
        "control_hash": (
            control_hashes[0].decode("ascii")
            if len(control_hashes) == 1
            else None
        ),
    }
    condition_values = [
        value
        for key, value in comparison.items()
        if key.startswith("condition_")
    ]
    comparison["all_success_conditions_pass"] = all(condition_values)
    write_json(evidence_dir / "comparison.json", comparison)
    if not comparison["all_success_conditions_pass"]:
        raise RuntimeError("one or more cross-platform success conditions failed")

    evidence = {
        "phase": "target",
        **identity,
        "native_target": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "rosetta_translated": False,
        },
        "source_pid_recorded": source_evidence["source_pid"],
        "source_exited_at": source_evidence["source_exited_at"],
        "source_process_exited": source_evidence["source_process_exited"],
        "source_process_reaped": source_evidence["source_process_reaped"],
        "target_pid": target_pid,
        "target_started_at": target_started_at,
        "target_started_unix_ns": target_started_ns,
        "target_exited_at": target_exited_at,
        "target_exited_unix_ns": target_exited_ns,
        "target_returncode": target.returncode,
        "image_sha256_source": source_evidence["image_sha256"],
        "image_sha256_before": before_hash,
        "image_sha256_after": after_hash,
        "final_hash": comparison["final_hash"],
        "control_hash": comparison["control_hash"],
        "combined_output_lines": comparison["combined_output_lines"],
        "combined_output_matches_control": comparison[
            "condition_14_combined_output_matches_control"
        ],
        "duplicate_irreversible_actions": comparison[
            "duplicate_irreversible_actions"
        ],
        "comparison_file": "comparison.json",
        "macos_environment_file": "macos-environment.txt",
        "full_test_file": "full-test-macos.txt",
        "recorded_at": utc_now(),
    }
    write_json(evidence_dir / "target-evidence.json", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    evidence_dir = args.input.resolve()
    try:
        evidence = perform(evidence_dir)
    except BaseException as exc:
        write_failure(evidence_dir, "macos-target", exc)
        # Preserve a post-attempt hash whenever the image is present.
        image_candidates = list(evidence_dir.glob("*.cont"))
        if len(image_candidates) == 1:
            image_hash = sha256_file(image_candidates[0])
            (evidence_dir / "image-target-after.sha256").write_text(
                f"{image_hash}  {image_candidates[0].name}\n",
                encoding="utf-8",
            )
        print(f"target validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
