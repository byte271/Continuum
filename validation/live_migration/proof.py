#!/usr/bin/env python3
"""Cross-OS, cross-ISA, cross-Python, cross-source-revision migration proof.

Source role: run revision A under one interpreter on one machine, freeze it
through the public CLI, and exit. Target role: on a different machine, OS,
architecture, and interpreter, introduce revision B, build and verify a
migration plan, and resume the unchanged image under the new revision.

The oracle is stated in this file, independently of the migration
implementation: the exact revision-A prefix, the exact revision-B suffix, the
migration boundary, the required action nonces, the forbidden old-revision
markers, and the required new-revision markers. The migration code does not get
to define its own expected result.
"""

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
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum import _harness  # noqa: E402
from continuum.abi import normalized_architecture  # noqa: E402

# ---------------------------------------------------------------------------
# The oracle. Stated here, derived from the program text by hand, never from a
# migration plan or from the implementation under test.
# ---------------------------------------------------------------------------

LIMIT = 30
# Tally seeds at 7 and adds bias(index) = index + 3 for each index in 0..29.
EXPECTED_FINAL_TOTAL = 7 + sum(index + 3 for index in range(LIMIT))
ACTION = re.compile(r"^ACTION (\d+) (\d+) (\d+)$")
NEWMARK = re.compile(r"^NEWMARK (\d+)$")
FORBIDDEN_OLD_MARKER = re.compile(r"^FINAL \d+ \d+$")
REQUIRED_NEW_MARKER = re.compile(r"^FINAL-V2 (\d+) (\d+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cli(python: str) -> list[str]:
    return [python, "-m", "continuum"]


def environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPOSITORY)
    env["CONTINUUM_HOME"] = str(home)
    return env


def run(command: list[str], home: Path, allow_failure: bool = False):
    completed = subprocess.run(
        command, cwd=str(REPOSITORY), env=environment(home),
        capture_output=True, text=True,
    )
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr}"
        )
    return completed


def host_identity(python: str) -> dict[str, Any]:
    probe = (
        "import json,platform;print(json.dumps({"
        "'python_version': platform.python_version(),"
        "'os': platform.system(),'machine': platform.machine()}))"
    )
    identity = json.loads(
        subprocess.run(
            [python, "-c", probe], check=True, capture_output=True, text=True,
            cwd=str(REPOSITORY),
        ).stdout
    )
    identity["architecture"] = normalized_architecture(identity["machine"])
    return identity


def source_role(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    program = Path(args.revision_a).resolve()
    identity = host_identity(args.python)
    if args.expect_python and identity["python_version"] != args.expect_python:
        raise RuntimeError(
            f"source requires Python {args.expect_python}, got "
            f"{identity['python_version']}"
        )

    image = output / "revision-a.cont"
    home = output / "home"
    sync = output / "sync"
    sync.mkdir(parents=True, exist_ok=True)
    env = _harness.environment_for(
        sync, base=environment(home), hold_safe_point=args.hold_safe_point
    )
    process = subprocess.Popen(
        [*cli(args.python), "run", str(program)],
        cwd=str(REPOSITORY), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready = _harness.wait_for_ready(process, sync)
        evidence = _harness.freeze_held_source(
            cli(args.python), ready["session_id"], image, process, sync, ready,
            cwd=REPOSITORY, env=env,
        )
        if evidence["returncode"] != 0:
            raise RuntimeError(f"continuum freeze failed: {evidence['stderr']!r}")
        prefix, stderr = process.communicate(timeout=180)
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)
        raise
    exit_status = process.poll()
    if exit_status is None:
        raise RuntimeError("source process did not exit")

    inspected = run([*cli(args.python), "inspect", str(image)], home)
    (output / "revision_a.py").write_bytes(program.read_bytes())
    (output / "prefix.log").write_text(prefix, encoding="utf-8")
    (output / "source-stderr.log").write_text(stderr, encoding="utf-8")
    write_json(
        output / "source-evidence.json",
        {
            "role": "source",
            "repository_commit": args.commit,
            "host": identity,
            "image": {
                "name": image.name,
                "sha256_at_capture": sha256_file(image),
                "bytes": image.stat().st_size,
            },
            "source_process": {
                "pid": process.pid,
                "exit_status": exit_status,
                # Measured, not asserted. `process.poll()` returns a status only
                # for a process that has terminated and been reaped, and the
                # raise above already refuses to continue when it does not. A
                # literal True here made both downstream assertions -- in the
                # workflow and in the final report -- tautologies.
                "exited_and_reaped_before_target": exit_status is not None,
                "session_id": ready["session_id"],
            },
            "freeze": {
                key: value for key, value in evidence.items()
                if key not in {"stdout", "stderr"}
            },
            "inspect_stdout": inspected.stdout,
            "revision_a_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
            "prefix_lines": len(prefix.splitlines()),
            "cli_only": True,
        },
    )
    print(json.dumps({"image_sha256": sha256_file(image)}, indent=2))
    return 0


def check_oracle(prefix: str, suffix: str) -> tuple[list[str], dict[str, Any]]:
    """Judge the hybrid run against the independently stated oracle."""

    failures: list[str] = []
    prefix_lines = prefix.splitlines()
    suffix_lines = suffix.splitlines()
    combined = prefix_lines + suffix_lines

    prefix_actions = [line for line in prefix_lines if ACTION.match(line)]
    suffix_actions = [line for line in suffix_lines if ACTION.match(line)]
    all_actions = prefix_actions + suffix_actions

    # Every action nonce exactly once, and every index covered exactly once.
    indexes = [int(ACTION.match(line).group(1)) for line in all_actions]
    if sorted(indexes) != list(range(LIMIT)):
        failures.append(f"action indexes are not exactly 0..{LIMIT - 1}: {sorted(indexes)}")
    if len(set(all_actions)) != len(all_actions):
        failures.append("an action nonce appeared more than once")
    repeated = sorted(set(prefix_actions) & set(suffix_actions))
    if repeated:
        failures.append(f"completed actions repeated: {repeated[:5]}")

    # The boundary: the prefix must be a strict, non-empty, non-total prefix.
    if not prefix_actions:
        failures.append("the source performed no work before the migration")
    if not suffix_actions:
        failures.append("the target performed no work after the migration")

    # Old revision's future behavior must not run; the new one's must.
    if any(FORBIDDEN_OLD_MARKER.match(line) for line in combined):
        failures.append("the old revision's future marker FINAL was executed")
    new_markers = [line for line in combined if REQUIRED_NEW_MARKER.match(line)]
    if len(new_markers) != 1:
        failures.append(
            f"expected exactly one FINAL-V2 marker, saw {len(new_markers)}"
        )
    else:
        total, count = REQUIRED_NEW_MARKER.match(new_markers[0]).groups()
        if int(total) != EXPECTED_FINAL_TOTAL:
            failures.append(
                f"final total {total} != oracle {EXPECTED_FINAL_TOTAL}"
            )
        if int(count) != LIMIT:
            failures.append(
                f"shared list length {count} != {LIMIT}; the shared alias or "
                "the reference cycle did not survive"
            )

    # New-revision-only behavior must appear only after the boundary.
    if any(NEWMARK.match(line) for line in prefix_lines):
        failures.append("new-revision behavior leaked into the revision-A prefix")
    marks = [line for line in suffix_lines if NEWMARK.match(line)]
    if len(marks) != len(suffix_actions):
        failures.append(
            f"NEWMARK count {len(marks)} does not match post-migration "
            f"iterations {len(suffix_actions)}"
        )

    digest = hashlib.sha256(
        ("\n".join(combined) + "\n").encode("utf-8")
    ).hexdigest()
    return failures, {
        "prefix_actions": len(prefix_actions),
        "suffix_actions": len(suffix_actions),
        "total_actions": len(all_actions),
        "completed_actions_repeated": len(repeated),
        "new_revision_markers_after_boundary": len(marks),
        "old_future_behavior_executed": any(
            FORBIDDEN_OLD_MARKER.match(line) for line in combined
        ),
        "new_future_behavior_executed": bool(new_markers),
        "expected_final_total": EXPECTED_FINAL_TOTAL,
        "combined_output_digest": digest,
    }


def target_role(args: argparse.Namespace) -> int:
    source_dir = Path(args.input).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = host_identity(args.python)
    if args.expect_python and identity["python_version"] != args.expect_python:
        raise RuntimeError(
            f"target requires Python {args.expect_python}, got "
            f"{identity['python_version']}"
        )

    evidence = json.loads((source_dir / "source-evidence.json").read_text())
    image = source_dir / evidence["image"]["name"]
    arrival = sha256_file(image)
    if arrival != evidence["image"]["sha256_at_capture"]:
        raise RuntimeError("image changed in transit")

    home = output / "home"
    revision_b = Path(args.revision_b).resolve()
    plan_path = output / "migration.cup"

    # Only now is revision B introduced; the image already exists.
    planned = run(
        [*cli(args.python), "plan-upgrade", str(image), str(revision_b),
         "-o", str(plan_path)],
        home,
    )
    verified = run(
        [*cli(args.python), "verify-upgrade", str(image), str(plan_path)], home
    )
    inspected = run([*cli(args.python), "inspect-upgrade", str(plan_path)], home)
    resumed = run(
        [*cli(args.python), "resume", str(image), "--upgrade", str(plan_path)],
        home,
    )

    after = sha256_file(image)
    prefix = (source_dir / "prefix.log").read_text(encoding="utf-8")
    failures, oracle = check_oracle(prefix, resumed.stdout)

    plan = json.loads(
        subprocess.run(
            [args.python, "-c",
             "import json,sys;sys.path.insert(0,'.');"
             "from continuum.migration import read_plan;"
             f"print(json.dumps(read_plan({str(plan_path)!r})[0]))"],
            check=True, capture_output=True, text=True, cwd=str(REPOSITORY),
            env=environment(home),
        ).stdout
    )

    report = {
        "role": "target",
        "repository_commit": args.commit,
        "source": {
            "os": evidence["host"]["os"],
            "architecture": evidence["host"]["architecture"],
            "python_version": evidence["host"]["python_version"],
            "exited_and_reaped_before_target": evidence["source_process"][
                "exited_and_reaped_before_target"
            ],
        },
        "target": {
            "os": identity["os"],
            "architecture": identity["architecture"],
            "python_version": identity["python_version"],
        },
        "cross_os": evidence["host"]["os"] != identity["os"],
        "cross_architecture": (
            evidence["host"]["architecture"] != identity["architecture"]
        ),
        "cross_python": (
            evidence["host"]["python_version"] != identity["python_version"]
        ),
        "cross_source_revision": (
            evidence["revision_a_sha256"]
            != hashlib.sha256(revision_b.read_bytes()).hexdigest()
        ),
        "image": {
            "sha256_at_capture": evidence["image"]["sha256_at_capture"],
            "sha256_on_arrival": arrival,
            "sha256_after_migration": after,
            "byte_identical_in_transit": arrival
            == evidence["image"]["sha256_at_capture"],
            "unchanged_by_migration": after == arrival,
        },
        "migration": {
            "plan_format_version": plan["plan_format_version"],
            "execution_abi_version": plan["execution_abi_version"],
            "active_frames_mapped": plan["active_frames"],
            "bindings_mapped": len(plan["binding_mappings"]),
            "classes_mapped": len(plan["class_mappings"]),
            "mapping_is_total": plan["mapping_is_total"],
            "accepted_edit_classes": plan["accepted_edit_classes"],
            "plan_stdout": planned.stdout,
            "verify_stdout": verified.stdout,
            "inspect_stdout": inspected.stdout,
            "resume_stderr": resumed.stderr,
        },
        "oracle": oracle,
        "oracle_failures": failures,
        "cli_only": True,
    }
    (output / "suffix.log").write_text(resumed.stdout, encoding="utf-8")
    (output / "combined.log").write_text(prefix + resumed.stdout, encoding="utf-8")
    write_json(output / "source-evidence.json", evidence)

    if not report["image"]["byte_identical_in_transit"]:
        failures.append("image was not byte-identical in transit")
    if not report["image"]["unchanged_by_migration"]:
        failures.append("the original image was modified")
    if plan["active_frames"] < 4:
        failures.append(f"only {plan['active_frames']} active frames mapped")
    if not plan["mapping_is_total"]:
        failures.append("the mapping was not total")

    # Written last, so the archived artifact records every failure. Serializing
    # before these four appends stored "oracle_failures": [] for a run that
    # failed for exactly those reasons -- CI still failed correctly, but the
    # retained evidence contradicted it.
    report["oracle_failures"] = failures
    write_json(output / "final-report.json", report)

    print(json.dumps(report, indent=2, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"PROOF FAILURE: {failure}", file=sys.stderr)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    roles = root.add_subparsers(dest="role", required=True)

    source = roles.add_parser("source")
    source.add_argument("--python", default=sys.executable)
    source.add_argument("--revision-a", required=True)
    source.add_argument("--output", required=True)
    source.add_argument("--hold-safe-point", type=int, required=True)
    source.add_argument("--expect-python", default="")
    source.add_argument("--commit", default="")
    source.set_defaults(handler=source_role)

    target = roles.add_parser("target")
    target.add_argument("--python", default=sys.executable)
    target.add_argument("--revision-b", required=True)
    target.add_argument("--input", required=True)
    target.add_argument("--output", required=True)
    target.add_argument("--expect-python", default="")
    target.add_argument("--commit", default="")
    target.set_defaults(handler=target_role)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
