from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import IR_VERSION, SUPPORTED_PYTHON, __version__
from .compiler import compile_source
from .errors import ContinuumError, FrozenExecution
from .image import inspect_image, load_image, verify_image
from .resources import is_portable_absolute_path
from .session import (
    SessionController,
    continuum_home,
    list_sessions,
    request_freeze,
)
from .vm import VirtualMachine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum",
        description="Save and restore supported pure-Python execution state.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"continuum {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run source in the controlled runtime")
    run.add_argument(
        "--file-policy", choices=("strict", "bundle"), default="strict"
    )
    run.add_argument("program")
    run.add_argument("arguments", nargs=argparse.REMAINDER)

    subparsers.add_parser("sessions", help="list known Continuum sessions")

    doctor = subparsers.add_parser(
        "doctor", help="report runtime identity and compatibility"
    )
    doctor.add_argument(
        "--json", action="store_true", help="emit a machine-readable report"
    )

    demo = subparsers.add_parser(
        "demo", help="run a same-machine continuation demonstration"
    )
    demo.add_argument(
        "--output-dir",
        help="new or empty directory in which to retain demonstration evidence",
    )
    demo.add_argument(
        "--iterations",
        type=int,
        default=20_000,
        help="workload iterations (default: 20000)",
    )

    freeze = subparsers.add_parser("freeze", help="freeze a running session")
    freeze.add_argument("session_id")
    freeze.add_argument("-o", "--output", required=True)

    inspect = subparsers.add_parser(
        "inspect", help="validate container metadata and inspect an image"
    )
    inspect.add_argument("image")

    verify = subparsers.add_parser(
        "verify", help="deeply verify an image without resuming it"
    )
    verify.add_argument("image")

    resume = subparsers.add_parser("resume", help="restore an image in this process")
    resume.add_argument("image")
    resume.add_argument(
        "--file-policy", choices=("strict", "relocate", "bundle"), default=None
    )
    resume.add_argument(
        "--relocate",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="map an original absolute file path to a target path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "sessions":
            return _sessions()
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "demo":
            return _demo(args)
        if args.command == "freeze":
            return _freeze(args)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "verify":
            return _verify(args)
        if args.command == "resume":
            return _resume(args)
        raise AssertionError(args.command)
    except ContinuumError as exc:
        print(f"continuum: error: {exc}", file=sys.stderr)
        return 2


def _require_runtime_version() -> None:
    current = platform.python_version()
    if current != SUPPORTED_PYTHON:
        raise ContinuumError(
            f"this Continuum runtime requires Python {SUPPORTED_PYTHON}; current is {current}"
        )


def _run(args: argparse.Namespace) -> int:
    _require_runtime_version()
    path = Path(args.program).expanduser().resolve()
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContinuumError(f"cannot read {path}: {exc}") from exc
    ir = compile_source(source, str(path))
    controller = SessionController(source, str(path))
    controller.start()
    vm = VirtualMachine(
        ir,
        [str(path), *args.arguments],
        str(path),
        resource_policy=args.file_policy,
        safe_point_callback=controller.on_safe_point,
    )
    print(f"Continuum session: {controller.session_id}", file=sys.stderr, flush=True)
    try:
        vm.run()
    except FrozenExecution:
        controller.finish("frozen")
        return 0
    except BaseException as exc:
        controller.finish("failed", str(exc))
        raise
    controller.finish("completed")
    return 0


def _sessions() -> int:
    sessions = list_sessions()
    if not sessions:
        print("No Continuum sessions.")
        return 0
    print(f"{'SESSION':<18} {'STATUS':<10} {'PID':<8} PROGRAM")
    for record in sessions:
        print(
            f"{record.get('session_id', '?'):<18} "
            f"{record.get('status', '?'):<10} "
            f"{str(record.get('pid', '?')):<8} "
            f"{record.get('program', '?')}"
        )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    current_python = platform.python_version()
    current_system = platform.system()
    current_machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(platform.machine().lower(), platform.machine().lower())
    manifest_path = os.environ.get("CONTINUUM_BUNDLE_MANIFEST")
    bundle_manifest = None
    problems = []

    if current_python != SUPPORTED_PYTHON:
        problems.append(
            f"Python {current_python} is incompatible; exact "
            f"CPython {SUPPORTED_PYTHON} is required"
        )
    if current_system not in {"Linux", "Darwin", "Windows"}:
        problems.append(f"unsupported operating system: {current_system}")
    if current_machine not in {"x86_64", "arm64"}:
        problems.append(f"unsupported architecture: {current_machine}")

    if manifest_path:
        path = Path(manifest_path)
        try:
            bundle_manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"cannot read runtime bundle manifest: {exc}")
        else:
            expected = {
                "continuum_version": __version__,
                "ir_version": IR_VERSION,
                "python_version": SUPPORTED_PYTHON,
                "system": current_system,
                "architecture": current_machine,
                "self_contained": True,
            }
            for key, value in expected.items():
                if bundle_manifest.get(key) != value:
                    problems.append(
                        f"runtime bundle manifest {key} mismatch: "
                        f"expected {value!r}, got {bundle_manifest.get(key)!r}"
                    )

    report = {
        "continuum_version": __version__,
        "continuum_ir_version": IR_VERSION,
        "python_implementation": platform.python_implementation(),
        "python_version": current_python,
        "required_python_version": SUPPORTED_PYTHON,
        "os": current_system,
        "architecture": current_machine,
        "continuum_home": str(continuum_home()),
        "capture_file_policies": ["strict", "bundle"],
        "restore_file_policies": ["strict", "relocate", "bundle"],
        "compatible_image_targets": [
            "Linux x86_64",
            "Linux arm64",
            "macOS x86_64",
            "macOS arm64",
            "Windows x86_64",
        ],
        "verified_migration": (
            "IR 0.2 Linux x86_64 -> macOS arm64 at "
            "15bceefece050d06a1f504244a77434e31fd5228"
        ),
        "current_runtime_cross_platform": "unverified",
        "self_contained": bundle_manifest is not None,
        "bundle_manifest": manifest_path,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"Continuum version: {report['continuum_version']} "
            f"(IR {report['continuum_ir_version']})"
        )
        print(
            "Runtime: "
            f"{report['python_implementation']} {report['python_version']} "
            f"(required {report['required_python_version']})"
        )
        print(f"Host: {report['os']} {report['architecture']}")
        print(
            "Accepted image target metadata: "
            + ", ".join(report["compatible_image_targets"])
        )
        print(f"Verified historical migration: {report['verified_migration']}")
        print(
            "Current runtime cross-platform proof: "
            + report["current_runtime_cross_platform"]
        )
        print(f"Continuum home: {report['continuum_home']}")
        print(
            "Resource policies: capture "
            + ", ".join(report["capture_file_policies"])
            + "; restore "
            + ", ".join(report["restore_file_policies"])
        )
        print(
            "Self-contained installation: "
            + ("yes" if report["self_contained"] else "no")
        )
        if problems:
            print("Compatibility: FAILED")
            for problem in problems:
                print(f"- {problem}")
        else:
            print("Compatibility: OK")
    return 2 if problems else 0


def _demo(args: argparse.Namespace) -> int:
    _require_runtime_version()
    if args.iterations < 1_000:
        raise ContinuumError("demo requires at least 1000 iterations")

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
    else:
        output_dir = (
            continuum_home() / "demos" / f"demo-{uuid.uuid4().hex[:12]}"
        )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ContinuumError(f"demo output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    examples = _examples_directory()
    program = output_dir / "demo.py"
    input_path = output_dir / "demo-input.txt"
    control_input = output_dir / "control-input.txt"
    try:
        shutil.copyfile(examples / "demo.py", program)
        shutil.copyfile(examples / "demo_input.txt", input_path)
        shutil.copyfile(examples / "demo_input.txt", control_input)
    except OSError as exc:
        raise ContinuumError(f"cannot prepare demo workload: {exc}") from exc

    image = output_dir / "demo.cont"
    source_stdout = output_dir / "source-stdout.log"
    source_stderr = output_dir / "source-stderr.log"
    target_stdout = output_dir / "target-stdout.log"
    target_stderr = output_dir / "target-stderr.log"
    control_stdout = output_dir / "control-stdout.log"
    control_stderr = output_dir / "control-stderr.log"
    combined_output = output_dir / "combined-output.log"
    comparison_path = output_dir / "comparison.json"
    command = [sys.executable, "-m", "continuum"]
    environment = os.environ.copy()
    environment["CONTINUUM_HOME"] = str(output_dir / "source-home")
    runtime_root = str(Path(__file__).resolve().parents[1])
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        runtime_root
        if not inherited_pythonpath
        else os.pathsep.join((runtime_root, inherited_pythonpath))
    )

    print("Same-machine continuation demonstration")
    print(f"Evidence directory: {output_dir}")
    print(f"Program: {program}")
    print(f"Runtime: Python {SUPPORTED_PYTHON} / Continuum IR {IR_VERSION}")

    with (
        source_stdout.open("w", encoding="utf-8") as stdout_handle,
        source_stderr.open("w", encoding="utf-8") as stderr_handle,
    ):
        source = subprocess.Popen(
            [
                *command,
                "run",
                "--file-policy",
                "bundle",
                str(program),
                str(input_path),
                str(args.iterations),
            ],
            cwd=output_dir,
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        session_id = _wait_for_demo_session(source, source_stderr, source_stdout)
        print("Continuum session started")
        print(f"Session: {session_id}")
        print(f"Source PID: {source.pid}")
        print("Freeze command:")
        print(f"  continuum freeze {session_id} -o {image}")

        freeze = subprocess.run(
            [*command, "freeze", session_id, "-o", str(image)],
            cwd=output_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if freeze.returncode != 0:
            if source.poll() is None:
                source.kill()
                source.wait()
            raise ContinuumError(
                "demo freeze failed: "
                + (freeze.stderr.strip() or freeze.stdout.strip())
            )
        source_returncode = source.wait(timeout=15)
        if source_returncode != 0:
            raise ContinuumError(
                f"demo source exited with status {source_returncode}"
            )
        source_exited_before_target = source.poll() is not None

    input_path.unlink()
    inspect_result = subprocess.run(
        [*command, "inspect", str(image)],
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if inspect_result.returncode != 0:
        raise ContinuumError(
            "demo image inspection failed: " + inspect_result.stderr.strip()
        )
    print("Checkpoint committed")
    print(inspect_result.stdout.rstrip())
    print("Source process exited")
    print("Original bundled input deleted")

    target_environment = {
        **environment,
        "CONTINUUM_HOME": str(output_dir / "target-home"),
    }
    with (
        target_stdout.open("w", encoding="utf-8") as stdout_handle,
        target_stderr.open("w", encoding="utf-8") as stderr_handle,
    ):
        target = subprocess.Popen(
            [*command, "resume", str(image), "--file-policy", "bundle"],
            cwd=output_dir,
            env=target_environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        target_pid = target.pid
        target_returncode = target.wait(timeout=60)
    if target_returncode != 0:
        raise ContinuumError(
            "demo resume failed: "
            + target_stderr.read_text(encoding="utf-8").strip()
        )

    control_environment = {
        **environment,
        "CONTINUUM_HOME": str(output_dir / "control-home"),
    }
    with (
        control_stdout.open("w", encoding="utf-8") as stdout_handle,
        control_stderr.open("w", encoding="utf-8") as stderr_handle,
    ):
        control = subprocess.run(
            [
                *command,
                "run",
                "--file-policy",
                "bundle",
                str(program),
                str(control_input),
                str(args.iterations),
            ],
            cwd=output_dir,
            env=control_environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            timeout=60,
        )
    if control.returncode != 0:
        raise ContinuumError(
            "demo control run failed: "
            + control_stderr.read_text(encoding="utf-8").strip()
        )

    before = source_stdout.read_bytes()
    after = target_stdout.read_bytes()
    control_bytes = control_stdout.read_bytes()
    combined = before + after
    combined_output.write_bytes(combined)
    resumed_hash = _demo_final_hash(combined)
    control_hash = _demo_final_hash(control_bytes)
    source_progress = _demo_progress(before)
    target_progress = _demo_progress(after)
    comparison = {
        "source_pid": source.pid,
        "source_returncode": source_returncode,
        "target_pid": target_pid,
        "target_returncode": target_returncode,
        "source_exited_before_target": source_exited_before_target,
        "new_target_process": True,
        "pid_values_differ": target_pid != source.pid,
        "original_input_absent": not input_path.exists(),
        "combined_output_matches_control": combined == control_bytes,
        "combined_output_bytes": len(combined),
        "control_output_bytes": len(control_bytes),
        "source_progress_last": source_progress[-1] if source_progress else None,
        "target_progress_first": target_progress[0] if target_progress else None,
        "resumed_final_hash": resumed_hash,
        "control_final_hash": control_hash,
        "final_hash_matches": resumed_hash == control_hash,
        "identity_proof_once": combined.count(b"IDENTITY True True\n") == 1,
        "final_output_once": combined.count(b"FINAL ") == 1,
    }
    comparison_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(
        comparison[key]
        for key in (
            "new_target_process",
            "source_exited_before_target",
            "original_input_absent",
            "combined_output_matches_control",
            "final_hash_matches",
            "identity_proof_once",
            "final_output_once",
        )
    ):
        raise ContinuumError(f"demo comparison failed; see {comparison_path}")

    print("Continuation restored")
    print(f"Target PID: {target_pid}")
    print(f"Last source progress: {comparison['source_progress_last']}")
    print(f"First resumed progress: {comparison['target_progress_first']}")
    print("Combined output matches uninterrupted control: yes")
    print(f"Final result hash: {resumed_hash}")
    print("Final result hash matches control: yes")
    print(f"Evidence retained in: {output_dir}")
    return 0


def _examples_directory() -> Path:
    bundle_manifest = os.environ.get("CONTINUUM_BUNDLE_MANIFEST")
    if bundle_manifest:
        candidate = Path(bundle_manifest).resolve().parent / "examples"
    else:
        candidate = Path(__file__).resolve().parents[1] / "examples"
    if not (candidate / "demo.py").is_file() or not (
        candidate / "demo_input.txt"
    ).is_file():
        raise ContinuumError(f"bundled demo workload is missing from {candidate}")
    return candidate


def _wait_for_demo_session(
    process: subprocess.Popen[str],
    stderr_path: Path,
    stdout_path: Path,
) -> str:
    deadline = time.monotonic() + 20
    session_id = None
    while time.monotonic() < deadline:
        stderr = stderr_path.read_text(encoding="utf-8")
        if match := re.search(r"^Continuum session: (cont-[0-9a-f]+)$", stderr, re.M):
            session_id = match.group(1)
        stdout = stdout_path.read_text(encoding="utf-8")
        if session_id and "Processing " in stdout:
            return session_id
        if process.poll() is not None:
            raise ContinuumError(
                "demo source exited before a checkpoint request could be made"
            )
        time.sleep(0.01)
    raise ContinuumError("timed out waiting for the demo safe point")


def _demo_progress(content: bytes) -> list[str]:
    return [
        line.decode("utf-8")
        for line in content.splitlines()
        if line.startswith(b"Processing ")
    ]


def _demo_final_hash(content: bytes) -> str | None:
    matches = re.findall(rb"^FINAL ([0-9a-f]{64})$", content, re.MULTILINE)
    if len(matches) != 1:
        return None
    return matches[0].decode("ascii")


def _freeze(args: argparse.Namespace) -> int:
    response = request_freeze(args.session_id, args.output)
    location = response["resume_location"]
    print(
        f"Frozen {args.session_id} at "
        f"{location['file']}:{location['line']} -> {response['image']}"
    )
    return 0


def _inspect(args: argparse.Namespace) -> int:
    report = inspect_image(args.image)
    manifest = report["manifest"]
    source = manifest["source"]
    location = manifest["resume_location"]
    print(f"Continuum Image: {args.image}")
    print(f"Format version: {manifest['format_version']}")
    print(f"Source OS: {source['os']}")
    print(f"Source architecture: {source['architecture']}")
    print(f"Python version: {source['python_version']}")
    print(f"Frames: {manifest['frames']}")
    print(f"Heap objects: {manifest['heap_objects']}")
    print(f"Open files: {manifest['open_files']}")
    print(f"Resume location: {location['file']}:{location['line']}")
    print(f"Unsupported resources: {len(manifest['unsupported_resources'])}")
    print(f"Integrity: {report['integrity']}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    _require_runtime_version()
    report = verify_image(args.image)
    manifest = report["manifest"]
    print(f"Continuum Image: {args.image}")
    print("Verification: passed")
    print(f"Integrity: {report['integrity']}")
    print(f"Compatibility: {report['compatibility']}")
    print(f"Object graph: {report['graph']}")
    print(f"Frames: {report['frames']} ({manifest['frames']})")
    print(f"Resources: {report['resources']} ({manifest['open_files']})")
    print("Execution: not started")
    return 0


def _resume(args: argparse.Namespace) -> int:
    _require_runtime_version()
    relocations = {}
    for mapping in args.relocate:
        if "=" not in mapping:
            raise ContinuumError(f"invalid relocation {mapping!r}; expected OLD=NEW")
        old, new = mapping.split("=", 1)
        if not old or not new:
            raise ContinuumError(f"invalid relocation {mapping!r}; expected OLD=NEW")
        if not is_portable_absolute_path(old):
            raise ContinuumError(
                f"invalid relocation {mapping!r}; OLD must be an absolute "
                "POSIX or Windows path"
            )
        relocations[old] = str(
            Path(new).expanduser().resolve()
        )
    loaded = load_image(args.image)
    loaded.validate_compatibility()
    compatibility = loaded.manifest["target_compatibility"]
    current_architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(platform.machine().lower(), platform.machine().lower())
    print(
        "Compatibility accepted: "
        f"runtime {compatibility['runtime_version']}, "
        f"Python {compatibility['python_version']}, "
        f"{platform.system()} {current_architecture}, "
        "portable IR with no native payload.",
        file=sys.stderr,
        flush=True,
    )
    vm = loaded.restore_vm(args.file_policy, relocations)
    source = loaded.manifest["source"]
    print(
        f"Restored from {source['os']} {source['architecture']}.",
        file=sys.stderr,
        flush=True,
    )
    vm.run()
    return 0
