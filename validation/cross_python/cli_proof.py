#!/usr/bin/env python3
"""Cross-Python continuation proof driven entirely through the public CLI.

This proves the Phase 1 capability using only commands a user can run:
`continuum run`, `continuum freeze`, `continuum verify`, `continuum resume`.
No private image reader, no in-process VM driving, and no proof-only
compatibility path. The source and target roles run as separate processes under
separate interpreters, and the source is fully exited and reaped before the
target reads the image.

Synchronization uses the shared safe-point hold primitive rather than watching
for output, so the checkpoint lands at the same execution position on every
host regardless of speed. The freeze itself is still an ordinary external
`continuum freeze` client observing a genuinely published request.

The workload is supplied as a file rather than embedded here, and the
checkpoint is a safe-point index rather than a predicate over program
variables, so nothing in this harness recognizes any particular program. The
replay check follows from that: it compares line multiplicities against the
uninterrupted control rather than assuming the workload emits globally unique
lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum import _harness  # noqa: E402
from continuum.abi import (  # noqa: E402
    CONTAINER_FORMAT_VERSION,
    EXECUTION_ABI_VERSION,
    normalized_architecture,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def cli(python: str) -> list[str]:
    return [python, "-m", "continuum"]


def host_identity(python: str) -> dict[str, Any]:
    """Ask the named interpreter to describe itself, rather than assuming."""

    probe = (
        "import json,platform,sys;"
        "print(json.dumps({"
        "'python_version': platform.python_version(),"
        "'python_implementation': platform.python_implementation(),"
        "'os': platform.system(),"
        "'machine': platform.machine(),"
        "'executable': sys.executable}))"
    )
    output = subprocess.run(
        [python, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY),
    ).stdout
    identity = json.loads(output)
    identity["architecture"] = normalized_architecture(identity["machine"])
    return identity


def environment(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPOSITORY)
    env["CONTINUUM_HOME"] = str(home)
    if extra:
        env.update(extra)
    return env


def run_control(python: str, program: Path, home: Path) -> dict[str, Any]:
    """Run the workload to completion, uninterrupted, as the oracle.

    This is an independent execution of the same program through the same public
    entry point. It never reads the image and never shares state with the
    checkpointed run.
    """

    completed = subprocess.run(
        [*cli(python), "run", str(program)],
        cwd=str(REPOSITORY),
        env=environment(home),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"control run failed ({completed.returncode}): {completed.stderr}"
        )
    return {"stdout": completed.stdout, "stderr": completed.stderr}


def freeze_source(
    python: str,
    program: Path,
    image: Path,
    home: Path,
    sync_dir: Path,
    hold_safe_point: int,
) -> dict[str, Any]:
    """Run the program under the public CLI and freeze it from outside.

    Returns evidence about the source process, including proof that it had
    exited and been reaped before this function returned.
    """

    sync_dir.mkdir(parents=True, exist_ok=True)
    env = _harness.environment_for(
        sync_dir,
        base=environment(home),
        hold_safe_point=hold_safe_point,
    )
    source = subprocess.Popen(
        [*cli(python), "run", str(program)],
        cwd=str(REPOSITORY),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _harness.wait_for_ready(source, sync_dir)
        session_id = ready["session_id"]
        freeze_evidence = _harness.freeze_held_source(
            cli(python),
            session_id,
            image,
            source,
            sync_dir,
            ready,
            cwd=REPOSITORY,
            env=env,
        )
        if freeze_evidence["returncode"] != 0:
            raise RuntimeError(
                f"continuum freeze failed: {freeze_evidence['stderr']}"
            )
        stdout, stderr = source.communicate(timeout=180)
    except BaseException:
        if source.poll() is None:
            source.kill()
            source.communicate(timeout=30)
        raise

    # The source has been waited on. Its exit status is available, which is
    # only true of a process that has terminated and been reaped.
    exit_status = source.poll()
    if exit_status is None:
        raise RuntimeError("source process did not exit")

    return {
        "session_id": session_id,
        "pid": source.pid,
        "exit_status": exit_status,
        "exited_and_reaped_before_target": True,
        "stdout": stdout,
        "stderr": stderr,
        "freeze": {
            key: value
            for key, value in freeze_evidence.items()
            if key not in {"stdout", "stderr"}
        },
        # freeze_held_source pipes bytes; decode for the JSON evidence record.
        "freeze_stdout": _text(freeze_evidence["stdout"]),
        "freeze_stderr": _text(freeze_evidence["stderr"]),
    }


def _text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def inspect_image(python: str, image: Path, home: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [*cli(python), "inspect", str(image)],
        cwd=str(REPOSITORY),
        env=environment(home),
        capture_output=True,
        text=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def verify_image(python: str, image: Path, home: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [*cli(python), "verify", str(image)],
        cwd=str(REPOSITORY),
        env=environment(home),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"continuum verify failed: {completed.stderr}")
    return {"stdout": completed.stdout, "stderr": completed.stderr}


def resume_image(python: str, image: Path, home: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [*cli(python), "resume", str(image)],
        cwd=str(REPOSITORY),
        env=environment(home),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"continuum resume failed: {completed.stderr}")
    return {"stdout": completed.stdout, "stderr": completed.stderr}


def source_role(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    program = Path(args.program).resolve()
    identity = host_identity(args.python)
    if args.expect_python and identity["python_version"] != args.expect_python:
        raise RuntimeError(
            f"source requires Python {args.expect_python}, got "
            f"{identity['python_version']}"
        )

    image = output / "source.cont"
    home = output / "home"
    evidence = freeze_source(
        args.python,
        program,
        image,
        home,
        output / "sync",
        args.hold_safe_point,
    )
    image_sha = sha256_file(image)

    # The control is produced on the source interpreter and carried alongside
    # the image so the target compares against a run it did not influence.
    control = run_control(args.python, program, output / "control-home")
    inspected = inspect_image(args.python, image, home)

    (output / "program.py").write_bytes(program.read_bytes())
    (output / "source-stdout.log").write_text(evidence["stdout"], encoding="utf-8")
    (output / "source-stderr.log").write_text(evidence["stderr"], encoding="utf-8")
    (output / "control-stdout.log").write_text(control["stdout"], encoding="utf-8")

    write_json(
        output / "source-evidence.json",
        {
            "role": "source",
            "repository_commit": args.commit,
            "container_format_version": CONTAINER_FORMAT_VERSION,
            "execution_abi_version": EXECUTION_ABI_VERSION,
            "host": identity,
            "program_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
            "image": {
                "name": image.name,
                "sha256_at_capture": image_sha,
                "bytes": image.stat().st_size,
            },
            "source_process": {
                "session_id": evidence["session_id"],
                "pid": evidence["pid"],
                "exit_status": evidence["exit_status"],
                "exited_and_reaped_before_target": evidence[
                    "exited_and_reaped_before_target"
                ],
            },
            "freeze": evidence["freeze"],
            "freeze_stdout": evidence["freeze_stdout"],
            "inspect_stdout": inspected["stdout"],
            "hold_safe_point": args.hold_safe_point,
            "cli_only": True,
        },
    )
    print(json.dumps({"image_sha256": image_sha, "image": str(image)}, indent=2))
    return 0


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

    source_evidence = json.loads(
        (source_dir / "source-evidence.json").read_text(encoding="utf-8")
    )
    image = source_dir / source_evidence["image"]["name"]
    sha_on_arrival = sha256_file(image)
    if sha_on_arrival != source_evidence["image"]["sha256_at_capture"]:
        raise RuntimeError(
            "image changed in transit: "
            f"{source_evidence['image']['sha256_at_capture']} -> {sha_on_arrival}"
        )

    home = output / "home"
    verified = verify_image(args.python, image, home)
    inspected = inspect_image(args.python, image, home)
    resumed = resume_image(args.python, image, home)

    # The image must be untouched by verification and by resuming it.
    sha_after = sha256_file(image)

    source_stdout = (source_dir / "source-stdout.log").read_text(encoding="utf-8")
    control_stdout = (source_dir / "control-stdout.log").read_text(encoding="utf-8")
    combined = source_stdout + resumed["stdout"]

    # Replay check by multiplicity against the uninterrupted control, not by
    # set intersection between the two halves. Intersection reported a repeat
    # for any line the program legitimately emits on both sides of the
    # checkpoint -- a blank line, a banner, a separator, a repeated value --
    # so the check silently depended on the workload emitting globally unique
    # lines, which the harness explicitly does not get to assume. A line is
    # evidence of replay only when the interrupted run produces it more often
    # than the control did.
    combined_counts = Counter(line for line in combined.splitlines() if line.strip())
    control_counts = Counter(
        line for line in control_stdout.splitlines() if line.strip()
    )
    repeated = sorted(
        line
        for line, count in combined_counts.items()
        if count > control_counts.get(line, 0)
    )

    report = {
        "role": "target",
        "repository_commit": args.commit,
        "container_format_version": CONTAINER_FORMAT_VERSION,
        "execution_abi_version": EXECUTION_ABI_VERSION,
        "source": {
            "os": source_evidence["host"]["os"],
            "architecture": source_evidence["host"]["architecture"],
            "python_version": source_evidence["host"]["python_version"],
            "exited_and_reaped_before_target": source_evidence["source_process"][
                "exited_and_reaped_before_target"
            ],
        },
        "target": {
            "os": identity["os"],
            "architecture": identity["architecture"],
            "python_version": identity["python_version"],
        },
        "cross_python": (
            source_evidence["host"]["python_version"] != identity["python_version"]
        ),
        "cross_os": source_evidence["host"]["os"] != identity["os"],
        "cross_architecture": (
            source_evidence["host"]["architecture"] != identity["architecture"]
        ),
        "image": {
            "sha256_at_capture": source_evidence["image"]["sha256_at_capture"],
            "sha256_on_arrival": sha_on_arrival,
            "sha256_after_restore": sha_after,
            "byte_identical_in_transit": (
                sha_on_arrival == source_evidence["image"]["sha256_at_capture"]
            ),
            "unchanged_by_restore": sha_after == sha_on_arrival,
        },
        "restoration": {
            "verify_stdout": verified["stdout"],
            "inspect_stdout": inspected["stdout"],
            "resume_stderr": resumed["stderr"],
            "completed_actions_repeated": len(repeated),
            "repeated_lines": repeated,
            "combined_output_matches_control": combined == control_stdout,
            "prefix_is_control_prefix": control_stdout.startswith(source_stdout),
            "suffix_completes_control": control_stdout.endswith(resumed["stdout"]),
        },
        "cli_only": True,
    }
    write_json(output / "final-report.json", report)
    (output / "combined-stdout.log").write_text(combined, encoding="utf-8")
    (output / "resume-stdout.log").write_text(resumed["stdout"], encoding="utf-8")
    (output / "control-stdout.log").write_text(control_stdout, encoding="utf-8")
    write_json(output / "source-evidence.json", source_evidence)

    failures = []
    if not report["image"]["byte_identical_in_transit"]:
        failures.append("image was not byte-identical in transit")
    if not report["image"]["unchanged_by_restore"]:
        failures.append("image was modified by restore")
    if not report["source"]["exited_and_reaped_before_target"]:
        failures.append("source did not exit before the target resumed")
    if report["restoration"]["completed_actions_repeated"] != 0:
        failures.append(
            f"{report['restoration']['completed_actions_repeated']} completed "
            "actions repeated"
        )
    if not report["restoration"]["combined_output_matches_control"]:
        failures.append("source plus target output did not match the control")
    print(json.dumps(report, indent=2, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"PROOF FAILURE: {failure}", file=sys.stderr)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    roles = root.add_subparsers(dest="role", required=True)

    source = roles.add_parser("source", help="run and freeze through the public CLI")
    source.add_argument("--python", default=sys.executable)
    source.add_argument("--program", required=True)
    source.add_argument("--output", required=True)
    source.add_argument("--hold-safe-point", type=int, required=True)
    source.add_argument("--expect-python", default="")
    source.add_argument("--commit", default="")
    source.set_defaults(handler=source_role)

    target = roles.add_parser("target", help="verify and resume through the CLI")
    target.add_argument("--python", default=sys.executable)
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
