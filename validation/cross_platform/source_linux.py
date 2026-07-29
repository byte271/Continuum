#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from common import (
    EXPECTED_PYTHON,
    all_evidence_files,
    create_source_identity,
    git_identity,
    import_python_build_evidence,
    render_raw_commands,
    run_full_tests,
    sha256_file,
    utc_now,
    write_failure,
    write_file_manifest,
    write_json,
)
from qualification import qualify_linux_source


def container_markers() -> list[str]:
    markers = []
    for path in (Path("/.dockerenv"), Path("/run/.containerenv")):
        if path.exists():
            markers.append(str(path))
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup = ""
    for name in ("docker", "containerd", "kubepods", "lxc", "podman"):
        if name in cgroup.lower():
            markers.append(f"/proc/1/cgroup:{name}")
    try:
        detection = subprocess.run(
            ["systemd-detect-virt", "--container"],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        detection = None
    if (
        detection is not None
        and detection.returncode == 0
        and detection.stdout.strip() not in {"", "none"}
    ):
        markers.append(f"systemd-detect-virt:{detection.stdout.strip()}")
    return sorted(set(markers))


def emulation_markers() -> list[str]:
    observations = []
    for path in (
        Path("/proc/cpuinfo"),
        Path("/sys/class/dmi/id/product_name"),
        Path("/sys/class/dmi/id/sys_vendor"),
    ):
        try:
            observations.append(f"{path}:{path.read_text(encoding='utf-8')}")
        except OSError:
            pass
    for command in (["lscpu"], ["systemd-detect-virt", "--vm"]):
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError:
            continue
        observations.append(
            f"{' '.join(command)}:{completed.stdout}\n{completed.stderr}"
        )
    combined = "\n".join(observations).lower()
    markers = []
    for name in ("qemu", "tcg", "bochs"):
        if name in combined:
            markers.append(name)
    return sorted(set(markers))


def wait_for(
    path: Path, needle: str, process: subprocess.Popen[bytes], timeout: int
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"source exited before freeze; return code {process.returncode}"
            )
        try:
            if needle in path.read_text(encoding="utf-8"):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {needle!r}")


def inspect_live_image(image: Path) -> dict[str, object]:
    from continuum.image import load_image

    loaded = load_image(image)
    vm = loaded.restore_vm("bundle")
    try:
        frame_names = [
            vm.ir["functions"][frame.function_id]["name"] for frame in vm.frames
        ]
        workload_frame = vm.frames[frame_names.index("workload")]
        graph = workload_frame.locals["graph"]
        rng = workload_frame.locals["rng"]
        handle = workload_frame.locals["handle"]
        result = {
            "frame_names": frame_names,
            "shared_reference_preserved": graph["left"] is graph["right"],
            "cycle_preserved": graph["self"] is graph,
            "rng_is_random_random": isinstance(rng, random.Random),
            "file_offset": handle.tell(),
        }
    finally:
        for resource in vm.resources.files.values():
            resource.close()
    return result


def perform(args: argparse.Namespace, output: Path) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))
    if repository == output or repository in output.parents:
        raise RuntimeError(
            "evidence directory must be outside the Git working tree"
        )
    architecture = platform.machine().lower()
    markers = container_markers()
    emulation = emulation_markers()
    if platform.system() != "Linux" or architecture != "x86_64":
        raise RuntimeError("source validation requires Linux x86_64")
    if platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"source requires Python {EXPECTED_PYTHON}; "
            f"current is {platform.python_version()}"
        )

    identity = create_source_identity(repository, output)
    python_build_metadata: dict[str, object] = {}
    python_build_verified = False
    if args.python_build_evidence is not None:
        python_build_metadata, _ = import_python_build_evidence(
            repository=repository,
            source=args.python_build_evidence.resolve(),
            destination=output,
            label="linux",
            expected_system="Linux",
            expected_machine="x86_64",
            current_executable=sys.executable,
        )
        python_build_verified = True
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository),
        "CONTINUUM_HOME": str(output / "source-continuum-home"),
    }
    repository_archive = output / str(identity["repository_archive"])
    raw_environment, environment_results = render_raw_commands(
        [
            ["uname", "-a"],
            ["uname", "-m"],
            ["cat", "/proc/cpuinfo"],
            ["lscpu"],
            ["systemd-detect-virt", "--vm"],
            ["systemd-detect-virt", "--container"],
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
            [sys.executable, "-m", "continuum", "--version"],
            ["git", "rev-parse", "HEAD"],
            ["git", "status", "--porcelain=v1"],
            ["sha256sum", str(repository_archive)],
        ],
        repository,
        environment=environment,
    )
    (output / "linux-environment.txt").write_text(
        raw_environment, encoding="utf-8"
    )
    required_result_indexes = (0, 1, 6, 7, 8, 9, 10, 11)
    if any(
        environment_results[index].returncode != 0
        for index in required_result_indexes
    ):
        raise RuntimeError("required Linux environment command failed")
    if environment_results[1].stdout.strip() != "x86_64":
        raise RuntimeError("uname -m is not exactly x86_64")
    if environment_results[6].stdout.strip() != f"Python {EXPECTED_PYTHON}":
        raise RuntimeError("validation interpreter is not exactly Python 3.12.13")
    python_identity_lines = environment_results[7].stdout.splitlines()
    if python_identity_lines[:2] != ["Linux", "x86_64"]:
        raise RuntimeError("validation interpreter is not native Linux x86_64")
    if environment_results[10].stdout:
        raise RuntimeError("Git working tree is not clean")
    hardware_probes_verified = all(
        environment_results[index].returncode == 0
        for index in (2, 3, 4)
    )
    qualified, qualification_failures, github = qualify_linux_source(
        system=platform.system(),
        machine=platform.machine(),
        uname_machine=environment_results[1].stdout.strip(),
        python_version=platform.python_version(),
        python_system=python_identity_lines[0],
        python_machine=python_identity_lines[1],
        python_build_verified=python_build_verified,
        hardware_probes_verified=hardware_probes_verified,
        container_markers=markers,
        emulation_markers=emulation,
        git_commit=str(identity["git_commit"]),
        environment=os.environ,
    )
    qualification = {
        "qualified_native_linux_x86_64": qualified and not args.rehearsal,
        "failures": qualification_failures,
        "rehearsal": bool(args.rehearsal),
        "container_markers": markers,
        "emulation_markers": emulation,
        "github": github,
        "python_build_verified": python_build_verified,
        "hardware_probes_verified": hardware_probes_verified,
    }
    write_json(output / "linux-qualification.json", qualification)
    write_json(output / "github-linux-metadata.json", github)
    if qualification_failures and not args.rehearsal:
        raise RuntimeError(
            "native GitHub Linux qualification failed: "
            + "; ".join(qualification_failures)
        )

    run_full_tests(
        repository,
        output / "full-test-linux.txt",
        {
            **environment,
            "CONTINUUM_HOME": str(output / "linux-test-home"),
        },
    )
    git_commit, _ = git_identity(repository)
    if git_commit != identity["git_commit"]:
        raise RuntimeError("Git commit changed during Linux validation")

    input_path = output / "proof-input.txt"
    control_input = output / "control-input.txt"
    shutil.copyfile(repository / "examples" / "demo_input.txt", input_path)
    shutil.copyfile(input_path, control_input)
    image = output / "linux-x86_64.cont"
    stdout_log = output / "source-stdout.log"
    stderr_log = output / "source-stderr.log"
    nonce = uuid.uuid4().hex
    source_started_at = utc_now()
    source_started_ns = time.time_ns()
    with stdout_log.open("wb", buffering=0) as stdout:
        source = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "continuum",
                "run",
                "--file-policy",
                "bundle",
                str(repository / "examples" / "cross_platform_proof.py"),
                str(input_path),
                str(args.iterations),
                nonce,
            ],
            cwd=repository,
            env=environment,
            stdout=stdout,
            stderr=subprocess.PIPE,
        )
        source_pid = source.pid
        if source.stderr is None:
            raise RuntimeError("source stderr pipe was not created")
        first_stderr = source.stderr.readline()
        if not first_stderr.startswith(b"Continuum session: "):
            raise RuntimeError(
                "source did not report a session: "
                + first_stderr.decode("utf-8", errors="replace")
            )
        session_id = first_stderr.decode("utf-8").strip().split(": ", 1)[1]
        wait_for(stdout_log, f"ACTION {nonce} ITER 30", source, 120)
        os.fsync(stdout.fileno())
        freeze = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum",
                "freeze",
                session_id,
                "-o",
                str(image),
            ],
            cwd=repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        (output / "freeze-stdout.log").write_bytes(freeze.stdout)
        (output / "freeze-stderr.log").write_bytes(freeze.stderr)
        if freeze.returncode != 0:
            source.kill()
            source.wait(timeout=30)
            raise RuntimeError(
                "freeze failed: "
                + freeze.stderr.decode("utf-8", errors="replace")
            )
        source_returncode = source.wait(timeout=60)
        source_exited_ns = time.time_ns()
        source_exited_at = utc_now()
        remaining_stderr = source.stderr.read()
        source.stderr.close()
        os.fsync(stdout.fileno())
    stderr_log.write_bytes(first_stderr + remaining_stderr)
    if source_returncode != 0:
        raise RuntimeError(f"source exited with {source_returncode}")
    source_reaped = source.poll() is not None
    if not source_reaped:
        raise RuntimeError("source process was not reaped")
    input_path.unlink()
    if input_path.exists():
        raise RuntimeError("original bundled input still exists")

    from continuum.image import inspect_image

    report = inspect_image(image)
    manifest = report["manifest"]
    frame_document: dict[str, object]
    import zipfile

    with zipfile.ZipFile(image, "r") as archive:
        frame_document = json.loads(archive.read("frames/frames.json"))
    frames = frame_document["frames"]
    live_state = inspect_live_image(image)
    operand_depth = sum(item["operand_stack_depth"] for item in frames)
    control_depth = sum(item["control_blocks"] for item in frames)
    file_records = manifest["supported_resources"]
    if manifest["frames"] < 4:
        raise RuntimeError("final image has fewer than four active frames")
    if operand_depth + control_depth == 0:
        raise RuntimeError("final image has no nonempty operand/control state")
    if not (
        live_state["shared_reference_preserved"]
        and live_state["cycle_preserved"]
        and live_state["rng_is_random_random"]
        and int(live_state["file_offset"]) > 0
    ):
        raise RuntimeError("final image is missing required portable live state")
    if len(file_records) < 1:
        raise RuntimeError("final image has no file resource")

    image_hash = sha256_file(image)
    image_size = image.stat().st_size
    (output / "image-source.sha256").write_text(
        f"{image_hash}  {image.name}\n", encoding="utf-8"
    )
    image.chmod(0o444)
    if sha256_file(image) != image_hash:
        raise RuntimeError("image changed while making it read-only")
    source_lines = stdout_log.read_text(encoding="utf-8").splitlines()
    final_git_commit, _ = git_identity(repository)
    if final_git_commit != git_commit:
        raise RuntimeError("Git commit changed while generating image")

    evidence = {
        "phase": "source",
        "qualified_native_linux_x86_64": qualified and not args.rehearsal,
        "rehearsal": bool(args.rehearsal),
        "container_markers": markers,
        "emulation_markers": emulation,
        "qualification_failures": qualification_failures,
        "github": github,
        "source_system": platform.system(),
        "source_machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "python_build": python_build_metadata,
        "python_build_verified": python_build_verified,
        "hardware_probes_verified": hardware_probes_verified,
        **identity,
        "source_tree_sha256_file": "source-tree.sha256",
        "source_pid": source_pid,
        "source_started_at": source_started_at,
        "source_started_unix_ns": source_started_ns,
        "source_exited_at": source_exited_at,
        "source_exited_unix_ns": source_exited_ns,
        "source_returncode": source_returncode,
        "source_process_exited": True,
        "source_process_reaped": source_reaped,
        "git_worktree_clean": True,
        "session_id": session_id,
        "freeze_location": manifest["resume_location"],
        "frame_count": manifest["frames"],
        "heap_object_count": manifest["heap_objects"],
        "file_resource_count": len(file_records),
        "operand_stack_items": operand_depth,
        "control_blocks": control_depth,
        "native_payload_required": manifest["target_compatibility"][
            "native_payload_required"
        ],
        "image": image.name,
        "image_bytes": image_size,
        "image_sha256": image_hash,
        "image_mode": oct(image.stat().st_mode & 0o777),
        "source_output_line_count": len(source_lines),
        "deleted_original_input": not input_path.exists(),
        "original_input_path": str(input_path),
        "nonce": nonce,
        "iterations": args.iterations,
        "live_state_checks": live_state,
        "source_environment_file": "linux-environment.txt",
        "full_test_file": "full-test-linux.txt",
        "recorded_at": utc_now(),
    }
    write_json(output / "source-evidence.json", evidence)
    source_evidence_files = all_evidence_files(
        output,
        exclude=("linux-evidence.sha256",),
    )
    write_file_manifest(
        output,
        source_evidence_files,
        "linux-evidence.sha256",
    )
    for name in (*source_evidence_files, "linux-evidence.sha256"):
        (output / name).chmod(0o444)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=6000)
    parser.add_argument("--python-build-evidence", type=Path)
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="allow a containerized dry run; resulting evidence is disqualified",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print("source validation requires a new empty evidence directory", file=sys.stderr)
        return 2
    try:
        evidence = perform(args, output)
    except BaseException as exc:
        write_failure(output, "linux-source", exc)
        print(f"source validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
