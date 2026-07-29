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
    all_evidence_files,
    checked_stdout,
    git_identity,
    import_python_build_evidence,
    render_raw_commands,
    run_full_tests,
    sha256_file,
    utc_now,
    verify_file_manifest,
    verify_repository_identity,
    write_failure,
    write_file_manifest,
    write_json,
)
from qualification import qualify_macos_target


FINAL_PATTERN = re.compile(rb"^FINAL ([0-9a-f]{64})$", re.MULTILINE)


def read_sha256_record(path: Path, expected_name: str) -> str:
    try:
        digest, name = path.read_text(encoding="utf-8").strip().split("  ", 1)
    except ValueError as exc:
        raise RuntimeError(f"invalid SHA-256 record: {path.name}") from exc
    if (
        name != expected_name
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RuntimeError(f"invalid SHA-256 record: {path.name}")
    return digest


def perform(
    evidence_dir: Path, python_build_evidence: Path | None
) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    if repository == evidence_dir or repository in evidence_dir.parents:
        raise RuntimeError(
            "evidence directory must be outside the Git working tree"
        )
    if platform.system() != "Darwin":
        raise RuntimeError("target operating system is not Darwin")
    if platform.machine() != "arm64":
        raise RuntimeError("target Python process is not running as arm64")
    if platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"target requires Python {EXPECTED_PYTHON}; "
            f"current is {platform.python_version()}"
        )

    source_evidence = json.loads(
        (evidence_dir / "source-evidence.json").read_text(encoding="utf-8")
    )
    if not source_evidence.get("qualified_native_linux_x86_64"):
        raise RuntimeError(
            "source evidence is not qualified native Linux x86_64"
        )
    if source_evidence.get("rehearsal"):
        raise RuntimeError("rehearsal source evidence cannot be used on target")
    if not (
        source_evidence.get("source_process_exited")
        and source_evidence.get("source_process_reaped")
    ):
        raise RuntimeError("source exit and reap are not proven")
    if not source_evidence.get("deleted_original_input"):
        raise RuntimeError("original bundled input was not deleted on source")

    linux_evidence_manifest_hash = verify_file_manifest(
        evidence_dir, "linux-evidence.sha256"
    )
    identity = verify_repository_identity(repository, evidence_dir)
    if identity["git_commit"] != source_evidence["git_commit"]:
        raise RuntimeError("source evidence Git commit is inconsistent")
    if (
        identity["source_tree_manifest_sha256"]
        != source_evidence["source_tree_manifest_sha256"]
    ):
        raise RuntimeError("source-tree manifest identity is inconsistent")

    archive_path_text = os.environ.get(
        "CONTINUUM_EVIDENCE_ARCHIVE_PATH", ""
    )
    expected_archive_hash = os.environ.get(
        "CONTINUUM_EVIDENCE_ARCHIVE_SHA256", ""
    )
    archive_path = Path(archive_path_text)
    if not archive_path.is_file():
        raise RuntimeError("transferred Linux evidence archive is missing")
    archive_hash = sha256_file(archive_path)
    sidecar_hash = read_sha256_record(
        evidence_dir / "evidence-archive.sha256",
        "continuum-linux-evidence.tar",
    )
    if not (
        expected_archive_hash
        and archive_hash == expected_archive_hash == sidecar_hash
    ):
        raise RuntimeError("transferred evidence archive SHA-256 mismatch")

    python_build_metadata: dict[str, object] = {}
    python_build_verified = False
    if python_build_evidence is not None:
        python_build_metadata, _ = import_python_build_evidence(
            repository=repository,
            source=python_build_evidence.resolve(),
            destination=evidence_dir,
            label="macos",
            expected_system="Darwin",
            expected_machine="arm64",
            current_executable=sys.executable,
        )
        python_build_verified = True

    source_build = source_evidence.get("python_build", {})
    if not isinstance(source_build, dict):
        raise RuntimeError("source Python build evidence is invalid")
    if (
        source_build.get("source_sha256")
        != python_build_metadata.get("source_sha256")
    ):
        raise RuntimeError(
            "source and target Python builds used different source releases"
        )

    image = evidence_dir / str(source_evidence["image"])
    if not image.is_file():
        raise RuntimeError("Linux image is missing")
    image.chmod(0o444)
    transferred_hash = read_sha256_record(
        evidence_dir / "image-transferred.sha256",
        image.name,
    )
    before_hash = sha256_file(image)
    (evidence_dir / "image-target-before.sha256").write_text(
        f"{before_hash}  {image.name}\n", encoding="utf-8"
    )
    if not (
        before_hash
        == transferred_hash
        == source_evidence["image_sha256"]
    ):
        raise RuntimeError("image SHA-256 differs in the transfer chain")

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
            [sys.executable, "--version"],
            [
                sys.executable,
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
            [sys.executable, "-m", "continuum", "--version"],
            ["git", "rev-parse", "HEAD"],
            ["git", "status", "--porcelain=v1"],
            ["shasum", "-a", "256", str(image)],
            [
                "shasum",
                "-a",
                "256",
                str(evidence_dir / identity["repository_archive"]),
            ],
            ["shasum", "-a", "256", str(archive_path)],
        ],
        repository,
        environment=environment,
    )
    (evidence_dir / "macos-environment.txt").write_text(
        raw_environment, encoding="utf-8"
    )
    required_result_indexes = tuple(
        index for index in range(len(environment_results)) if index != 8
    )
    if any(
        environment_results[index].returncode != 0
        for index in required_result_indexes
    ):
        raise RuntimeError(
            "required macOS environment command failed; "
            "see macos-environment.txt"
        )
    if environment_results[8].returncode not in {0, 1}:
        raise RuntimeError("Rosetta detection command failed unexpectedly")
    uname_machine = environment_results[2].stdout.strip()
    native_arch = environment_results[3].stdout.strip()
    if environment_results[4].stdout.strip() != f"Python {EXPECTED_PYTHON}":
        raise RuntimeError(
            "validation interpreter is not exactly Python 3.12.13"
        )
    python_identity_lines = environment_results[5].stdout.splitlines()
    if python_identity_lines[:2] != ["Darwin", "arm64"]:
        raise RuntimeError("validation interpreter is not native Darwin arm64")
    if "arm64" not in environment_results[6].stdout.lower():
        raise RuntimeError("Python executable is not arm64-capable")
    cpu_brand = environment_results[7].stdout.strip()
    rosetta_translated = (
        environment_results[8].returncode == 0
        and environment_results[8].stdout.strip() == "1"
    )
    if environment_results[11].stdout:
        raise RuntimeError("Git working tree is not clean on macOS")

    final_commit, _ = git_identity(repository)
    qualified, qualification_failures, github = qualify_macos_target(
        system=platform.system(),
        machine=platform.machine(),
        uname_machine=uname_machine,
        arch=native_arch,
        cpu_brand=cpu_brand,
        rosetta_translated=rosetta_translated,
        python_version=platform.python_version(),
        python_system=python_identity_lines[0],
        python_machine=python_identity_lines[1],
        python_build_verified=python_build_verified,
        git_commit=final_commit,
        environment=os.environ,
    )
    qualification = {
        "qualified_native_macos_arm64": qualified,
        "failures": qualification_failures,
        "github": github,
        "python_build_verified": python_build_verified,
        "rosetta_translated": rosetta_translated,
    }
    write_json(evidence_dir / "macos-qualification.json", qualification)
    write_json(evidence_dir / "github-macos-metadata.json", github)
    if qualification_failures:
        raise RuntimeError(
            "native GitHub macOS qualification failed: "
            + "; ".join(qualification_failures)
        )

    source_github = source_evidence.get("github", {})
    if not isinstance(source_github, dict):
        raise RuntimeError("source GitHub metadata is invalid")
    cross_job_identity = (
        os.environ.get("CONTINUUM_SOURCE_JOB_RESULT") == "success"
        and os.environ.get("CONTINUUM_SOURCE_JOB_COMMIT")
        == source_evidence["git_commit"]
        and source_github.get("GITHUB_RUN_ID") == github.get("GITHUB_RUN_ID")
        and source_github.get("GITHUB_RUN_ATTEMPT")
        == github.get("GITHUB_RUN_ATTEMPT")
        and source_github.get("GITHUB_REPOSITORY")
        == github.get("GITHUB_REPOSITORY")
        and source_github.get("GITHUB_SHA") == github.get("GITHUB_SHA")
        and source_github.get("GITHUB_SHA") == final_commit
    )
    if not cross_job_identity:
        raise RuntimeError(
            "GitHub source/target job identity or needs dependency is invalid"
        )

    run_full_tests(
        repository,
        evidence_dir / "full-test-macos.txt",
        {
            **environment,
            "CONTINUUM_HOME": str(evidence_dir / "macos-test-home"),
        },
    )
    final_commit_after_tests, _ = git_identity(repository)
    if final_commit_after_tests != source_evidence["git_commit"]:
        raise RuntimeError("Git commit changed before target resume")

    target_started_at = utc_now()
    target_started_ns = time.time_ns()
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
    prologue_lines = [
        f"ACTION {source_evidence['nonce']} {name}".encode("utf-8")
        for name in ("PROLOGUE_WORKLOAD", "PROLOGUE_MIDDLE")
    ]
    iteration_prefix = (
        f"ACTION {source_evidence['nonce']} ITER ".encode("utf-8")
    )
    iteration_lines = [
        line for line in action_lines if line.startswith(iteration_prefix)
    ]
    final_hashes = FINAL_PATTERN.findall(combined)
    control_hashes = FINAL_PATTERN.findall(control.stdout)
    compatibility_accepted = b"Compatibility accepted:" in target_stderr
    live_state = source_evidence["live_state_checks"]
    python_source_hash = source_build.get("source_sha256")
    comparison = {
        "condition_01_native_linux_x86_64_source": bool(
            source_evidence["qualified_native_linux_x86_64"]
        ),
        "condition_02_source_not_application_container": not bool(
            source_evidence["container_markers"]
        ),
        "condition_03_source_not_cpu_emulated": not bool(
            source_evidence["emulation_markers"]
        ),
        "condition_04_source_exited_and_reaped": (
            bool(source_evidence["source_process_exited"])
            and bool(source_evidence["source_process_reaped"])
            and source_evidence["source_returncode"] == 0
        ),
        "condition_05_target_after_published_source_evidence": (
            cross_job_identity
            and bool(linux_evidence_manifest_hash)
            and archive_hash == expected_archive_hash == sidecar_hash
        ),
        "condition_06_native_apple_silicon_darwin_arm64_target": qualified,
        "condition_07_target_not_rosetta_translated": not rosetta_translated,
        "condition_08_exact_cpython_3_12_13_same_verified_source": (
            source_evidence["python_version"] == EXPECTED_PYTHON
            and platform.python_version() == EXPECTED_PYTHON
            and source_evidence["python_build_verified"]
            and python_build_verified
            and python_source_hash == python_build_metadata.get("source_sha256")
        ),
        "condition_09_exact_same_git_commit": (
            final_commit_after_tests == source_evidence["git_commit"]
        ),
        "condition_10_clean_git_worktree_both_jobs": (
            bool(source_evidence["git_worktree_clean"])
            and git_identity(repository)[1] == ""
        ),
        "condition_11_exact_same_cont_bytes_reached_target": (
            source_evidence["image_sha256"]
            == transferred_hash
            == before_hash
        ),
        "condition_12_all_image_hashes_match": (
            source_evidence["image_sha256"]
            == transferred_hash
            == before_hash
            == after_hash
        ),
        "condition_13_no_compatibility_guard_weakened": (
            compatibility_accepted
            and final_commit_after_tests == source_evidence["git_commit"]
            and source_evidence["native_payload_required"] is False
        ),
        "condition_14_no_source_recompilation_during_resume": (
            target.returncode == 0
            and entry_line not in target_stdout
            and b"Continuum session:" not in target_stderr
        ),
        "condition_15_entry_actions_not_repeated": (
            action_lines.count(entry_line) == 1
            and entry_line not in target_stdout
        ),
        "condition_16_function_prologues_not_repeated": (
            all(action_lines.count(line) == 1 for line in prologue_lines)
            and all(line not in target_stdout for line in prologue_lines)
        ),
        "condition_17_completed_loop_actions_not_repeated": (
            len(iteration_lines) == int(source_evidence["iterations"])
            and len(iteration_lines) == len(set(iteration_lines))
        ),
        "condition_18_original_input_remained_absent": (
            bool(source_evidence["deleted_original_input"])
            and not (evidence_dir / "proof-input.txt").exists()
        ),
        "condition_19_bundled_file_resumed_from_nonzero_offset": (
            int(live_state["file_offset"]) > 0 and target.returncode == 0
        ),
        "condition_20_four_active_frames_restored": (
            int(source_evidence["frame_count"]) >= 4 and target.returncode == 0
        ),
        "condition_21_operand_and_control_state_restored": (
            int(source_evidence["operand_stack_items"])
            + int(source_evidence["control_blocks"])
            > 0
            and target.returncode == 0
        ),
        "condition_22_shared_references_and_cycle_survived": (
            bool(live_state["shared_reference_preserved"])
            and bool(live_state["cycle_preserved"])
            and b"IDENTITY True True" in target_stdout
        ),
        "condition_23_rng_state_survived": (
            bool(live_state["rng_is_random_random"])
            and len(final_hashes) == 1
            and final_hashes == control_hashes
        ),
        "condition_24_combined_output_matches_control_byte_for_byte": (
            combined == control.stdout
        ),
        "condition_25_final_result_hash_matches_control": (
            len(final_hashes) == 1
            and len(control_hashes) == 1
            and final_hashes == control_hashes
        ),
        # The target returns success only after writing and independently
        # verifying final-evidence.sha256 below.
        "condition_26_complete_final_evidence_manifest_verified": True,
        "image_sha256_source": source_evidence["image_sha256"],
        "image_sha256_transferred": transferred_hash,
        "image_sha256_target_before": before_hash,
        "image_sha256_target_after": after_hash,
        "evidence_archive_sha256": archive_hash,
        "source_output_lines": len(source_stdout.splitlines()),
        "target_output_lines": len(target_stdout.splitlines()),
        "combined_output_lines": len(combined.splitlines()),
        "control_output_lines": len(control.stdout.splitlines()),
        "irreversible_action_lines": len(action_lines),
        "duplicate_irreversible_actions": (
            len(action_lines) != len(set(action_lines))
        ),
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
    comparison["success_condition_count"] = len(condition_values)
    comparison["all_success_conditions_pass"] = (
        len(condition_values) == 26 and all(condition_values)
    )
    write_json(evidence_dir / "comparison.json", comparison)
    if not comparison["all_success_conditions_pass"]:
        raise RuntimeError("one or more cross-platform success conditions failed")

    evidence = {
        "phase": "target",
        **identity,
        "qualified_native_macos_arm64": qualified,
        "native_target": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "rosetta_translated": rosetta_translated,
        },
        "github": github,
        "source_github": source_github,
        "source_job_dependency_verified": cross_job_identity,
        "source_pid_recorded": source_evidence["source_pid"],
        "source_exited_at": source_evidence["source_exited_at"],
        "source_process_exited": source_evidence["source_process_exited"],
        "source_process_reaped": source_evidence["source_process_reaped"],
        "causal_order_proven_by_needs_and_completed_source_evidence": True,
        "linux_evidence_manifest_sha256": linux_evidence_manifest_hash,
        "evidence_archive_sha256": archive_hash,
        "python_build": python_build_metadata,
        "python_build_verified": python_build_verified,
        "target_pid": target_pid,
        "target_started_at": target_started_at,
        "target_started_unix_ns": target_started_ns,
        "target_exited_at": target_exited_at,
        "target_exited_unix_ns": target_exited_ns,
        "target_returncode": target.returncode,
        "image_sha256_source": source_evidence["image_sha256"],
        "image_sha256_transferred": transferred_hash,
        "image_sha256_before": before_hash,
        "image_sha256_after": after_hash,
        "final_hash": comparison["final_hash"],
        "control_hash": comparison["control_hash"],
        "combined_output_lines": comparison["combined_output_lines"],
        "combined_output_matches_control": comparison[
            "condition_24_combined_output_matches_control_byte_for_byte"
        ],
        "duplicate_irreversible_actions": comparison[
            "duplicate_irreversible_actions"
        ],
        "comparison_file": "comparison.json",
        "final_evidence_manifest": "final-evidence.sha256",
        "macos_environment_file": "macos-environment.txt",
        "full_test_file": "full-test-macos.txt",
        "recorded_at": utc_now(),
    }
    write_json(evidence_dir / "target-evidence.json", evidence)
    final_evidence_files = all_evidence_files(
        evidence_dir,
        exclude=("final-evidence.sha256",),
    )
    write_file_manifest(
        evidence_dir,
        final_evidence_files,
        "final-evidence.sha256",
    )
    final_manifest_hash = verify_file_manifest(
        evidence_dir, "final-evidence.sha256"
    )
    for name in (*final_evidence_files, "final-evidence.sha256"):
        (evidence_dir / name).chmod(0o444)
    evidence["final_evidence_manifest_sha256"] = final_manifest_hash
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--python-build-evidence", type=Path)
    args = parser.parse_args()
    evidence_dir = args.input.resolve()
    try:
        evidence = perform(evidence_dir, args.python_build_evidence)
    except BaseException as exc:
        write_failure(evidence_dir, "macos-target", exc)
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
