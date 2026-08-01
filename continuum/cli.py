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
from typing import Any

from . import IR_VERSION, SUPPORTED_PYTHON, __version__
from .abi import (
    CONTAINER_FORMAT_VERSION,
    EXECUTION_ABI_VERSION,
    GRAPH_CODEC_VERSION,
    LEGACY_CONTAINER_FORMAT_VERSION,
    POLICY_EXECUTION_ABI,
    TARGET_ARCHITECTURES,
    TARGET_OPERATING_SYSTEMS,
    VERIFIED_PLATFORMS,
    VERIFIED_PYTHON_VERSIONS,
    normalized_architecture,
)
from ._harness import (
    DEFAULT_HOLD_SAFE_POINT,
    HOLD_SAFE_POINT_ENV,
    SYNC_ENV,
    release as harness_release,
    safe_point_callback as harness_safe_point_callback,
    wait_for_ready as harness_wait_for_ready,
    wait_for_request as harness_wait_for_request,
)
from .checkpoint import (
    FAILURE_CONTINUE,
    FAILURE_POLICIES,
    MIN_SLOTS,
    CheckpointScheduler,
    CheckpointStore,
    parse_interval,
    parse_slots,
)
from .compiler import compile_source
from .errors import ContinuumError, FrozenExecution
from .image import TARGET_PLATFORMS, inspect_image, load_image, verify_image
from .migration import (
    PLAN_FORMAT_VERSION,
    apply_plan,
    load_verified_plan,
    plan_upgrade as build_upgrade_plan,
    read_plan,
    sha256_file,
    verify_plan,
    write_plan,
)
from .resources import is_portable_absolute_path
from .session import (
    SessionController,
    read_published_json,
    continuum_home,
    list_sessions,
    request_freeze,
)
from .vm import VirtualMachine

_DISPLAY_OS = {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}

BUNDLE_WORKFLOW_URL = (
    "https://github.com/byte271/Continuum/actions/workflows/runtime-bundles.yml"
)
PROOF_WORKFLOW_URL = (
    "https://github.com/byte271/Continuum/actions/workflows/"
    "cross-platform-proof.yml"
)

# Hosts on which the complete suite, including checkpoint, source exit, and
# resume in a new process, runs natively in BUNDLE_WORKFLOW_URL.
VERIFIED_SAME_HOST_TARGETS = (
    "Linux x86_64",
    "macOS arm64",
    "Windows x86_64",
)
# Directions in which one host has written an image that another host resumed,
# proven by the two dependent jobs in PROOF_WORKFLOW_URL. A pair belongs here
# only after that workflow has run it end to end.
VERIFIED_CROSS_PLATFORM_PATHS = ("Linux x86_64 -> macOS arm64",)


def _platform_label(system: str, architecture: str) -> str:
    return f"{_DISPLAY_OS.get(system, system)} {architecture}"


DEMO_READY_TIMEOUT_SECONDS = 120.0
DEMO_START_TIMEOUT_SECONDS = 120.0
DEMO_REQUEST_TIMEOUT_SECONDS = 30.0
DEMO_POLL_INTERVAL_SECONDS = 0.005


def _format_compatible_targets() -> tuple[str, ...]:
    return tuple(
        _platform_label(entry["os"], entry["architecture"])
        for entry in TARGET_PLATFORMS
    )


def _add_checkpoint_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the rolling-checkpoint options.

    All default to off. A run without --checkpoint-dir behaves exactly as it did
    before this feature existed, including producing no checkpoint directory.
    """

    parser.add_argument(
        "--checkpoint-dir",
        default=None,
        metavar="DIR",
        help="enable rolling crash-recovery checkpoints in DIR",
    )
    parser.add_argument(
        "--checkpoint-interval",
        default="1s",
        metavar="DURATION",
        help="requested interval between checkpoints, such as 100ms, 1s, 5s "
        "(default: 1s). A target, not a guarantee: a checkpoint that takes "
        "longer than the interval delays the next one rather than overlapping.",
    )
    parser.add_argument(
        "--checkpoint-slots",
        type=int,
        default=MIN_SLOTS,
        metavar="N",
        help=f"committed checkpoint slots to rotate (default: {MIN_SLOTS})",
    )
    parser.add_argument(
        "--checkpoint-failure",
        choices=FAILURE_POLICIES,
        default=FAILURE_CONTINUE,
        help="what to do when a checkpoint cannot be committed "
        f"(default: {FAILURE_CONTINUE})",
    )


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
    _add_checkpoint_arguments(run)
    run.add_argument(
        "--recover-latest",
        action="store_true",
        help="resume from the newest valid checkpoint in --checkpoint-dir if one exists",
    )
    run.add_argument("program")
    run.add_argument("arguments", nargs=argparse.REMAINDER)

    subparsers.add_parser("sessions", help="list known Continuum sessions")

    recover = subparsers.add_parser(
        "recover",
        help="resume the newest valid checkpoint in a checkpoint directory",
    )
    recover.add_argument("checkpoint_dir")
    recover.add_argument(
        "--file-policy", choices=("strict", "relocate", "bundle"), default=None
    )
    recover.add_argument(
        "--relocate",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="map an original absolute file path to a target path",
    )
    recover.add_argument(
        "--lineage",
        default=None,
        help="require this lineage identifier instead of inferring it",
    )
    recover.add_argument(
        "--dry-run",
        action="store_true",
        help="report the selection without resuming execution",
    )

    checkpoints = subparsers.add_parser(
        "checkpoints",
        help="report the state of a checkpoint directory",
    )
    checkpoints.add_argument("checkpoint_dir")
    checkpoints.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )

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

    plan_upgrade = subparsers.add_parser(
        "plan-upgrade", help="plan a migration of an image onto new source"
    )
    plan_upgrade.add_argument("image")
    plan_upgrade.add_argument("new_program")
    plan_upgrade.add_argument("-o", "--output", required=True)

    inspect_upgrade = subparsers.add_parser(
        "inspect-upgrade", help="inspect a migration plan without applying it"
    )
    inspect_upgrade.add_argument("plan")

    verify_upgrade = subparsers.add_parser(
        "verify-upgrade",
        help="independently re-derive a migration plan and confirm it matches",
    )
    verify_upgrade.add_argument("image")
    verify_upgrade.add_argument("plan")

    resume = subparsers.add_parser("resume", help="restore an image in this process")
    resume.add_argument("image")
    resume.add_argument(
        "--upgrade",
        default=None,
        metavar="PLAN",
        help="apply a verified migration plan before resuming",
    )
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
        if args.command == "recover":
            return _recover(args)
        if args.command == "checkpoints":
            return _checkpoints(args)
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
        if args.command == "plan-upgrade":
            return _plan_upgrade(args)
        if args.command == "inspect-upgrade":
            return _inspect_upgrade(args)
        if args.command == "verify-upgrade":
            return _verify_upgrade(args)
        if args.command == "resume":
            return _resume(args)
        raise AssertionError(args.command)
    except ContinuumError as exc:
        print(f"continuum: error: {exc}", file=sys.stderr)
        return 2


def _require_runtime_version() -> None:
    """Refuse to operate on an interpreter this runtime has not verified.

    This replaces an equality check against a single hard-coded version. It is
    deliberately still an exact allowlist, not a range: membership in
    `VERIFIED_PYTHON_VERSIONS` requires a green native cross-Python proof run,
    so an interpreter nobody has exercised is refused before any execution state
    is created or reconstructed rather than attempted and hoped for.

    Install-time packaging metadata (`requires-python`) is necessarily coarser
    than an exact allowlist; this gate, not that metadata, is the authority.
    """

    current = platform.python_version()
    if current not in VERIFIED_PYTHON_VERSIONS:
        raise ContinuumError(
            f"this Continuum runtime has not verified Python {current}; verified "
            f"versions are {list(VERIFIED_PYTHON_VERSIONS)}"
        )


def _run(args: argparse.Namespace) -> int:
    # Argument validation first: a malformed invocation should say so plainly
    # rather than be masked by an interpreter message. It creates no execution
    # state, so it does not weaken the gate below.
    checkpoint_dir = getattr(args, "checkpoint_dir", None)
    recover_latest = getattr(args, "recover_latest", False)
    if recover_latest and not checkpoint_dir:
        raise ContinuumError("--recover-latest requires --checkpoint-dir")
    _require_runtime_version()
    path = Path(args.program).expanduser().resolve()
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContinuumError(f"cannot read {path}: {exc}") from exc

    store = None
    scheduler = None
    interval = 0.0
    if checkpoint_dir is not None:
        interval = parse_interval(args.checkpoint_interval)
        store = CheckpointStore(checkpoint_dir, slots=parse_slots(args.checkpoint_slots))
        # An interrupted commit can leave a temporary file behind. It is never a
        # recovery candidate, but removing it here keeps the directory bounded.
        for name in store.cleanup_temporaries():
            print(
                f"Removed stale checkpoint temporary: {name}",
                file=sys.stderr,
                flush=True,
            )

    resumed = None
    if recover_latest:
        result = store.recover()
        if result.selected is not None:
            resumed = result
            _report_recovery_selection(result)

    ir = compile_source(source, str(path))
    controller = SessionController(source, str(path))
    safe_point_callback = harness_safe_point_callback(controller)
    controller.start()
    if resumed is not None:
        loaded = load_image(resumed.selected.path)
        if loaded.manifest["entry_program_sha256"] != _sha256_text(source):
            raise ContinuumError(
                "checkpoint was produced by a different program than "
                f"{path}; refusing to resume it against changed source"
            )
        vm = loaded.restore_vm(
            args.file_policy, None, safe_point_callback
        )
    else:
        vm = VirtualMachine(
            ir,
            [str(path), *args.arguments],
            str(path),
            resource_policy=args.file_policy,
            safe_point_callback=safe_point_callback,
        )
    if store is not None:
        lineage = (
            resumed.lineage_id if resumed is not None else controller.session_id
        )
        scheduler = CheckpointScheduler(
            store,
            source,
            lineage_id=lineage,
            interval_seconds=interval,
            failure_policy=args.checkpoint_failure,
            on_event=_checkpoint_event_reporter(),
        )
        controller.attach_checkpoints(scheduler)
        print(
            f"Checkpoints: every {args.checkpoint_interval} into "
            f"{store.directory} ({store.slots} slots, lineage {lineage}, "
            f"on failure: {args.checkpoint_failure})",
            file=sys.stderr,
            flush=True,
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
    finally:
        # Stopping is safe here and only here: commits are synchronous, so
        # control never reaches this point with a checkpoint half written.
        if scheduler is not None:
            scheduler.stop()
    controller.finish("completed")
    return 0


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _checkpoint_event_reporter():
    def report(event: str, payload: dict[str, Any]) -> None:
        if event == "checkpoint-committed":
            durable = "durable" if payload["durable"] else "file-flushed-only"
            print(
                f"Checkpoint {payload['generation']} -> {payload['slot']} "
                f"({payload['image_bytes']} bytes, pause "
                f"{payload['pause_seconds'] * 1000:.1f}ms, commit "
                f"{payload['commit_seconds'] * 1000:.1f}ms, {durable})",
                file=sys.stderr,
                flush=True,
            )
        elif event == "checkpoint-failed":
            # Loud by default: a silent checkpoint failure is how a user ends up
            # believing they have recovery when they do not.
            print(
                f"continuum: checkpoint failed: {payload['error']}",
                file=sys.stderr,
                flush=True,
            )

    return report


def _report_recovery_selection(result: Any) -> None:
    selected = result.selected
    print(
        f"Recovering generation {selected.generation} from {selected.slot} "
        f"(lineage {selected.lineage_id}, written {selected.created_at})",
        file=sys.stderr,
        flush=True,
    )
    for refusal in result.refusals:
        print(f"  refused {refusal}", file=sys.stderr, flush=True)


def _recover(args: argparse.Namespace) -> int:
    _require_runtime_version()
    store = CheckpointStore(args.checkpoint_dir)
    result = store.recover(lineage_id=args.lineage)
    if result.selected is None:
        for refusal in result.refusals:
            print(f"  refused {refusal}", file=sys.stderr, flush=True)
        raise ContinuumError(
            f"no valid checkpoint found in {store.directory}"
        )
    _report_recovery_selection(result)
    if args.dry_run:
        return 0
    relocations = _parse_relocations(args.relocate)
    # Reuse the audited restore path rather than a second loader.
    loaded = load_image(result.selected.path)
    loaded.validate_compatibility()
    vm = loaded.restore_vm(args.file_policy, relocations)
    source = loaded.manifest["source"]
    print(
        f"Restored from {source['os']} {source['architecture']}.",
        file=sys.stderr,
        flush=True,
    )
    vm.run()
    return 0


def _checkpoints(args: argparse.Namespace) -> int:
    store = CheckpointStore(args.checkpoint_dir)
    result = store.recover()
    slots = []
    for item in store.inspect_slots():
        slots.append(
            {
                "slot": item.slot,
                "present": item.present,
                "valid": item.valid,
                "generation": item.generation,
                "lineage_id": item.lineage_id,
                "created_at": item.created_at,
                "previous_generation": item.previous_generation,
                "directory_fsync": item.directory_fsync,
                "reason": item.reason,
            }
        )
    selected = result.selected
    fallback = None
    if selected is not None:
        others = [
            item
            for item in store.inspect_slots()
            if item.valid and item.slot != selected.slot
        ]
        if others:
            fallback = max(others, key=lambda item: item.generation).slot
    report = {
        "directory": str(store.directory),
        "slots": slots,
        "slot_count": store.slots,
        "active_slot": selected.slot if selected else None,
        "fallback_slot": fallback,
        "last_generation": selected.generation if selected else None,
        "last_committed_at": selected.created_at if selected else None,
        "lineage_id": result.lineage_id,
        "requested_interval_seconds": None,
        "directory_fsync": store.directory_fsync,
        "refusals": list(result.refusals),
    }
    if selected is not None:
        loaded = load_image(selected.path)
        block = loaded.manifest["checkpoint"]
        report["requested_interval_seconds"] = block["requested_interval_seconds"]
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(f"Checkpoint directory: {report['directory']}")
    print(f"Configured slots: {report['slot_count']}")
    print(f"Lineage: {report['lineage_id'] or 'none'}")
    print(f"Active slot: {report['active_slot'] or 'none'}")
    print(f"Fallback slot: {report['fallback_slot'] or 'none'}")
    print(f"Last committed generation: {report['last_generation'] or 'none'}")
    print(f"Last committed at: {report['last_committed_at'] or 'none'}")
    interval = report["requested_interval_seconds"]
    print(
        "Requested interval: "
        + (f"{interval}s" if interval is not None else "unknown")
    )
    print(f"Directory flush: {report['directory_fsync']}")
    for item in slots:
        state = "valid" if item["valid"] else ("absent" if not item["present"] else "invalid")
        detail = f" generation {item['generation']}" if item["valid"] else ""
        reason = f" ({item['reason']})" if item["reason"] and not item["valid"] else ""
        print(f"  {item['slot']}: {state}{detail}{reason}")
    for refusal in report["refusals"]:
        print(f"  refused {refusal}")
    return 0


def _parse_relocations(mappings: list[str]) -> dict[str, str]:
    relocations = {}
    for mapping in mappings:
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
        relocations[old] = str(Path(new).expanduser().resolve())
    return relocations


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
    current_machine = normalized_architecture()
    manifest_path = os.environ.get("CONTINUUM_BUNDLE_MANIFEST")
    bundle_manifest = None
    problems = []

    if current_python not in VERIFIED_PYTHON_VERSIONS:
        problems.append(
            f"Python {current_python} is not verified by this runtime; verified "
            f"CPython versions are {list(VERIFIED_PYTHON_VERSIONS)}"
        )
    if current_system not in TARGET_OPERATING_SYSTEMS:
        problems.append(f"unsupported operating system: {current_system}")
    if current_machine not in TARGET_ARCHITECTURES:
        problems.append(f"unsupported architecture: {current_machine}")
    # The axis checks above accept the whole 3x2 product. Membership in both
    # axes is not membership in the pair set: Windows arm64 satisfies each axis
    # and is still refused at restore. `doctor` answers "will this host work?",
    # so it has to ask the same question the runtime does.
    if (current_system, current_machine) not in VERIFIED_PLATFORMS:
        problems.append(
            f"this runtime does not accept platform {current_system} "
            f"{current_machine}; accepted pairs are "
            f"{[f'{name} {machine}' for name, machine in VERIFIED_PLATFORMS]}"
        )

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

    current_target = _platform_label(current_system, current_machine)
    report = {
        "continuum_version": __version__,
        "continuum_ir_version": IR_VERSION,
        "python_implementation": platform.python_implementation(),
        "python_version": current_python,
        "required_python_version": SUPPORTED_PYTHON,
        "verified_python_versions": list(VERIFIED_PYTHON_VERSIONS),
        "container_format_version": CONTAINER_FORMAT_VERSION,
        "execution_abi_version": EXECUTION_ABI_VERSION,
        "graph_codec_version": GRAPH_CODEC_VERSION,
        "os": current_system,
        "architecture": current_machine,
        "continuum_home": str(continuum_home()),
        "capture_file_policies": ["strict", "bundle"],
        "restore_file_policies": ["strict", "relocate", "bundle"],
        # What this runtime will attempt to restore. Not evidence.
        "format_compatible_targets": list(_format_compatible_targets()),
        # Where a checkpoint has been resumed by a new process on the same
        # host, and between which hosts an image has actually moved.
        "verified_same_host_targets": list(VERIFIED_SAME_HOST_TARGETS),
        "verified_cross_platform_paths": list(VERIFIED_CROSS_PLATFORM_PATHS),
        "current_target": current_target,
        "current_target_same_host_verified": (
            current_target in VERIFIED_SAME_HOST_TARGETS
        ),
        "evidence": {
            "format_compatible_targets": (
                "image manifest execution_contract.target; a listed pair is "
                "accepted by this runtime and is not evidence that any "
                "continuation on that pair has been run"
            ),
            "verified_same_host_targets": BUNDLE_WORKFLOW_URL,
            "verified_cross_platform_paths": PROOF_WORKFLOW_URL,
        },
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
            "Format-compatible image targets: "
            + ", ".join(report["format_compatible_targets"])
        )
        print(
            "  accepted by this runtime; not evidence of a verified "
            "continuation path"
        )
        print(
            "Verified same-host continuation: "
            + ", ".join(report["verified_same_host_targets"])
        )
        print(f"  evidence: {BUNDLE_WORKFLOW_URL}")
        print(
            "Verified cross-platform continuation: "
            + ", ".join(report["verified_cross_platform_paths"])
        )
        print(f"  evidence: {PROOF_WORKFLOW_URL}")
        print(
            "This host: "
            + report["current_target"]
            + (
                "; same-host continuation verified"
                if report["current_target_same_host_verified"]
                else "; same-host continuation not verified on this target"
            )
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
    # Only the source process is synchronized. The resumed target and the
    # uninterrupted control must run exactly as an ordinary user would run
    # them, so they never receive these names.
    sync_dir = output_dir / "sync"
    sync_dir.mkdir()
    start_path = sync_dir / "start"
    ready_path = sync_dir / "ready.json"
    source_environment = {
        **environment,
        SYNC_ENV: str(sync_dir),
        HOLD_SAFE_POINT_ENV: str(DEFAULT_HOLD_SAFE_POINT),
    }

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
            env=source_environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        freeze_process = None
        try:
            ready = _wait_for_demo_ready(
                source, ready_path, source_stderr, source_stdout
            )
            session_id = ready["session_id"]
            print("Continuum session started")
            print(f"Session: {session_id}")
            print(f"Source PID: {source.pid}")
            print(
                "Source held at safe point "
                f"{ready['safe_points_executed']} awaiting a published "
                "freeze request"
            )
            print("Freeze command:")
            print(f"  continuum freeze {session_id} -o {image}")

            # The real freeze client, started before the source is released so
            # that no host can finish the workload first.
            freeze_process = subprocess.Popen(
                [*command, "freeze", session_id, "-o", str(image)],
                cwd=output_dir,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            request_published = _wait_for_freeze_request(
                freeze_process, source, Path(ready["request_path"])
            )
            source_alive_at_publication = source.poll() is None
            if not source_alive_at_publication:
                raise ContinuumError(
                    "demo source exited while held at its safe point; the "
                    "start gate did not hold the workload"
                )
            print("Freeze request published; releasing the held safe point")
            harness_release(sync_dir)

            try:
                freeze_stdout, freeze_stderr = freeze_process.communicate(
                    timeout=DEMO_START_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as exc:
                raise ContinuumError(
                    "demo freeze did not complete within "
                    f"{DEMO_START_TIMEOUT_SECONDS:.0f}s after the source was "
                    "released"
                ) from exc
            if freeze_process.returncode != 0:
                raise ContinuumError(
                    "demo freeze failed: "
                    + (freeze_stderr.strip() or freeze_stdout.strip())
                )
            source_returncode = source.wait(timeout=15)
            if source_returncode != 0:
                raise ContinuumError(
                    f"demo source exited with status {source_returncode}"
                )
            source_exited_before_target = source.poll() is not None
        finally:
            _terminate_demo_process(freeze_process)
            _terminate_demo_process(source)

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
    identity_count, final_count = _demo_marker_counts(combined)
    comparison = {
        "hold_safe_point": DEFAULT_HOLD_SAFE_POINT,
        "source_safe_points_at_hold": ready["safe_points_executed"],
        "freeze_request_published_before_release": request_published,
        "source_alive_when_request_published": source_alive_at_publication,
        "source_made_progress_before_freeze": bool(source_progress),
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
        "identity_proof_once": identity_count == 1,
        "final_output_once": final_count == 1,
    }
    comparison_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not source_progress:
        raise ContinuumError(
            f"demo source froze at safe point {DEFAULT_HOLD_SAFE_POINT} without "
            "producing progress output; re-measure DEFAULT_HOLD_SAFE_POINT "
            f"against the demonstration workload, see {comparison_path}"
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
            "freeze_request_published_before_release",
            "source_alive_when_request_published",
            "source_made_progress_before_freeze",
        )
    ):
        raise ContinuumError(f"demo comparison failed; see {comparison_path}")

    print("Continuation restored")
    print(f"Target PID: {target_pid}")
    print(
        "Freeze request published while the source was held: "
        + ("yes" if comparison["source_alive_when_request_published"] else "no")
    )
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


def _demo_output_tail(path: Path, limit: int = 500) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[-limit:]


def _wait_for_demo_ready(
    process: subprocess.Popen[str],
    ready_path: Path,
    stderr_path: Path,
    stdout_path: Path,
) -> dict[str, Any]:
    """Wait until the source is held at its safe point and has said so.

    Returns the readiness document the held source published. Waiting on that
    document rather than on observed output is what makes the demonstration
    independent of how fast the host runs the workload.
    """

    deadline = time.monotonic() + DEMO_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_path.exists():
            return _validate_demo_ready(ready_path, stderr_path)
        if process.poll() is not None:
            raise ContinuumError(
                "demo source exited before reaching its held safe point "
                f"(status {process.returncode}): "
                + (
                    _demo_output_tail(stderr_path)
                    or _demo_output_tail(stdout_path)
                    or "no output"
                )
            )
        time.sleep(DEMO_POLL_INTERVAL_SECONDS)
    raise ContinuumError(
        "timed out after "
        f"{DEMO_READY_TIMEOUT_SECONDS:.0f}s waiting for the demo source to "
        f"reach safe point {DEFAULT_HOLD_SAFE_POINT} and publish {ready_path}"
    )


def _validate_demo_ready(ready_path: Path, stderr_path: Path) -> dict[str, Any]:
    try:
        ready = read_published_json(ready_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuumError(
            f"cannot read demo readiness document {ready_path}: {exc}"
        ) from exc
    session_id = ready.get("session_id")
    request_path = ready.get("request_path")
    safe_points = ready.get("safe_points_executed")
    if not isinstance(session_id, str) or not re.fullmatch(
        r"cont-[0-9a-f]+", session_id
    ):
        raise ContinuumError(
            f"demo readiness document has no valid session: {session_id!r}"
        )
    if not isinstance(request_path, str) or not request_path:
        raise ContinuumError("demo readiness document has no freeze request path")
    if not isinstance(safe_points, int) or safe_points < 1:
        raise ContinuumError("demo readiness document has no safe-point count")

    # The documented interface is the session identifier on stderr. Check the
    # held source agrees with it instead of trusting one channel.
    stderr = ""
    try:
        stderr = stderr_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContinuumError(f"cannot read demo source stderr: {exc}") from exc
    match = re.search(r"^Continuum session: (cont-[0-9a-f]+)$", stderr, re.M)
    if match is None:
        raise ContinuumError(
            "demo source published readiness without announcing a session on "
            "stderr"
        )
    if match.group(1) != session_id:
        raise ContinuumError(
            "demo session identity disagreement: stderr reported "
            f"{match.group(1)}, readiness document reported {session_id}"
        )
    return ready


def _wait_for_freeze_request(
    freeze_process: subprocess.Popen[str],
    source: subprocess.Popen[str],
    request_path: Path,
) -> bool:
    """Wait until the real freeze client has published its request document."""

    deadline = time.monotonic() + DEMO_REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if request_path.exists():
            return True
        if freeze_process.poll() is not None:
            stdout, stderr = freeze_process.communicate()
            raise ContinuumError(
                "demo freeze client exited before publishing a request: "
                + (stderr.strip() or stdout.strip() or "no output")
            )
        if source.poll() is not None:
            raise ContinuumError(
                "demo source exited before the freeze request was published"
            )
        time.sleep(DEMO_POLL_INTERVAL_SECONDS)
    raise ContinuumError(
        f"freeze request {request_path} was not published within "
        f"{DEMO_REQUEST_TIMEOUT_SECONDS:.0f}s"
    )


def _release_demo_start(start_path: Path) -> None:
    try:
        descriptor = os.open(
            start_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError as exc:
        raise ContinuumError(
            f"demo start gate {start_path} was already released"
        ) from exc
    except OSError as exc:
        raise ContinuumError(
            f"cannot release demo start gate {start_path}: {exc}"
        ) from exc
    os.close(descriptor)


def _terminate_demo_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.kill()
    try:
        process.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError):
        pass


def _demo_progress(content: bytes) -> list[str]:
    return [
        line.decode("utf-8")
        for line in content.splitlines()
        if line.startswith(b"Processing ")
    ]


def _demo_marker_counts(content: bytes) -> tuple[int, int]:
    lines = content.splitlines()
    return (
        lines.count(b"IDENTITY True True"),
        sum(line.startswith(b"FINAL ") for line in lines),
    )


def _demo_final_hash(content: bytes) -> str | None:
    matches = [
        match.group(1)
        for line in content.splitlines()
        if (match := re.fullmatch(rb"FINAL ([0-9a-f]{64})", line))
    ]
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
    contract = manifest.get("execution_contract")
    if contract is not None:
        target = contract["target"]
        print(f"Execution ABI: {contract['execution_abi_version']}")
        print(f"Graph codec: {contract['graph_codec_version']}")
        print(f"IR version: {contract['ir_version']}")
        print(f"Compatibility policy: {contract['compatibility_policy']}")
        print(
            "Accepted target Python versions: "
            f"{', '.join(target['python_versions'])}"
        )
        print(
            "Required capabilities: "
            f"{', '.join(target['required_capabilities'])}"
        )
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
    contract = report["execution_contract"]
    print(f"Compatibility policy: {contract['compatibility_policy']}")
    if contract["compatibility_policy"] == POLICY_EXECUTION_ABI:
        print(f"Execution ABI: {contract['execution_abi_version']}")
        print(f"Creator Python: {contract['creator_python_version']}")
        print(f"Restoring Python: {report['restore_python_version']}")
    print(f"Object graph: {report['graph']}")
    print(f"Frames: {report['frames']} ({manifest['frames']})")
    print(f"Resources: {report['resources']} ({manifest['open_files']})")
    print("Execution: not started")
    return 0


def _plan_upgrade(args: argparse.Namespace) -> int:
    _require_runtime_version()
    plan = build_upgrade_plan(args.image, args.new_program)
    new_source = Path(args.new_program).read_text(encoding="utf-8")
    write_plan(
        args.output,
        plan,
        new_source,
        compile_source(new_source, plan["entry_program"]),
    )
    print(f"Migration plan: {args.output}")
    print(f"Plan format version: {plan['plan_format_version']}")
    print(f"Execution ABI: {plan['execution_abi_version']}")
    print(f"Original image SHA-256: {plan['original_image_sha256']}")
    print(f"New source SHA-256: {plan['new_source_sha256']}")
    print(f"Active frames mapped: {plan['active_frames']}")
    print(f"Active bindings mapped: {len(plan['binding_mappings'])}")
    print(f"Control regions mapped: {len(plan['control_region_mappings'])}")
    print(f"Classes mapped: {len(plan['class_mappings'])}")
    print(f"Accepted edit classes: {', '.join(plan['accepted_edit_classes']) or 'none'}")
    print(f"Mapping is total: {plan['mapping_is_total']}")
    return 0


def _inspect_upgrade(args: argparse.Namespace) -> int:
    plan, _new_source, _new_ir = read_plan(args.plan)
    print(f"Migration plan: {args.plan}")
    print(f"Plan format version: {plan['plan_format_version']}")
    print(f"Semantic model version: {plan['semantic_model_version']}")
    print(f"Execution ABI: {plan['execution_abi_version']}")
    print(f"Original image SHA-256: {plan['original_image_sha256']}")
    print(f"Old source SHA-256: {plan['old_source_sha256']}")
    print(f"Old IR SHA-256: {plan['old_ir_sha256']}")
    print(f"New source SHA-256: {plan['new_source_sha256']}")
    print(f"New IR SHA-256: {plan['new_ir_sha256']}")
    print(f"Active frames mapped: {plan['active_frames']}")
    print(f"Mapping is total: {plan['mapping_is_total']}")
    print(f"Accepted edit classes: {', '.join(plan['accepted_edit_classes']) or 'none'}")
    for mapping in plan["frame_mappings"]:
        scope = "/".join(mapping["evidence"]["old_function"]["scope_path"])
        print(
            f"  frame {mapping['frame_depth']} {scope}: "
            f"pc {mapping['old_pc']} -> {mapping['new_pc']}, "
            f"stack {mapping['operand_stack_depth']}, "
            f"blocks {len(mapping['control_blocks'])}"
        )
    for assumption in plan["assumptions"]:
        print(f"  assumption: {assumption}")
    return 0


def _verify_upgrade(args: argparse.Namespace) -> int:
    _require_runtime_version()
    report = verify_plan(args.image, args.plan)
    print(f"Migration plan: {args.plan}")
    print("Verification: passed")
    print(f"Integrity: {report['integrity']}")
    print(f"Independently re-derived: {report['independently_rederived']}")
    print(f"Execution ABI: {report['execution_abi_version']}")
    print(f"Original image SHA-256: {report['original_image_sha256']}")
    print(f"Active frames mapped: {report['active_frames']}")
    print(f"Mapping is total: {report['mapping_is_total']}")
    print(f"Execution: {report['execution']}")
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
    decision = loaded.validate_compatibility()
    current_architecture = normalized_architecture()
    if decision.get("compatibility_policy") == POLICY_EXECUTION_ABI:
        creator = decision["creator"]
        # Name both interpreters explicitly. When they differ, this line is the
        # operator-visible statement that a cross-Python restore was accepted on
        # ABI grounds rather than by matching the creator.
        print(
            "Compatibility accepted: "
            f"execution ABI {decision['execution_abi_version']}, "
            f"IR {decision['ir_version']}, "
            f"graph codec {decision['graph_codec_version']}, "
            f"restoring under Python {platform.python_version()} on "
            f"{platform.system()} {current_architecture}; "
            f"image created by Continuum {creator['continuum_version']} under "
            f"Python {creator['python_version']} on {creator['os']} "
            f"{creator['architecture']}; portable IR with no native payload.",
            file=sys.stderr,
            flush=True,
        )
    else:
        compatibility = loaded.manifest["target_compatibility"]
        print(
            "Compatibility accepted: "
            f"container format {LEGACY_CONTAINER_FORMAT_VERSION} exact-version "
            f"policy, runtime {compatibility['runtime_version']}, "
            f"Python {compatibility['python_version']}, "
            f"{platform.system()} {current_architecture}, "
            "portable IR with no native payload.",
            file=sys.stderr,
            flush=True,
        )
    vm = loaded.restore_vm(args.file_policy, relocations)
    upgrade = getattr(args, "upgrade", None)
    if upgrade:
        # Verify before applying, and apply the object that was verified. One
        # read: verifying a path and then reading it again would apply whatever
        # the second read returned, and a substituted plan that is internally
        # self-consistent for this image passes every check apply_plan makes.
        _report, plan, _new_source, new_ir = load_verified_plan(args.image, upgrade)
        apply_plan(vm, plan, new_ir)
        print(
            "Migration applied: "
            f"plan format {plan['plan_format_version']}, "
            f"{plan['active_frames']} active frames remapped, "
            f"new source SHA-256 {plan['new_source_sha256'][:16]}...; "
            "the original image is unmodified.",
            file=sys.stderr,
            flush=True,
        )
    source = loaded.manifest["source"]
    print(
        f"Restored from {source['os']} {source['architecture']}.",
        file=sys.stderr,
        flush=True,
    )
    vm.run()
    return 0
