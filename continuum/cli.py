from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from . import SUPPORTED_PYTHON, __version__
from .compiler import compile_source
from .errors import ContinuumError, FrozenExecution
from .image import inspect_image, load_image
from .session import SessionController, list_sessions, request_freeze
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

    freeze = subparsers.add_parser("freeze", help="freeze a running session")
    freeze.add_argument("session_id")
    freeze.add_argument("-o", "--output", required=True)

    inspect = subparsers.add_parser("inspect", help="verify and inspect an image")
    inspect.add_argument("image")

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
        if args.command == "freeze":
            return _freeze(args)
        if args.command == "inspect":
            return _inspect(args)
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
            f"this v0.1 prototype requires Python {SUPPORTED_PYTHON}; current is {current}"
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


def _resume(args: argparse.Namespace) -> int:
    _require_runtime_version()
    relocations = {}
    for mapping in args.relocate:
        if "=" not in mapping:
            raise ContinuumError(f"invalid relocation {mapping!r}; expected OLD=NEW")
        old, new = mapping.split("=", 1)
        if not old or not new:
            raise ContinuumError(f"invalid relocation {mapping!r}; expected OLD=NEW")
        relocations[str(Path(old).expanduser().resolve())] = str(
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
