from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable


EXPECTED_PYTHON = "3.12.13"
CPYTHON_BUILD_FILES = (
    "build-metadata.json",
    "build.log",
    "compiler.txt",
    "configure-command.txt",
    "python-executable.txt",
    "python-file.txt",
    "python-platform.txt",
    "python-version.txt",
    "source-url.txt",
    "source.sha256",
)


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def expected_cpython_source_sha256(repository: Path) -> str:
    checksum_path = (
        repository
        / "validation"
        / "cross_platform"
        / "cpython-3.12.13.sha256"
    )
    line = checksum_path.read_text(encoding="utf-8").strip()
    try:
        digest, filename = line.split("  ", 1)
    except ValueError as exc:
        raise RuntimeError("invalid pinned CPython checksum file") from exc
    if (
        filename != "Python-3.12.13.tar.xz"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RuntimeError("invalid pinned CPython 3.12.13 source identity")
    return digest


def import_python_build_evidence(
    *,
    repository: Path,
    source: Path,
    destination: Path,
    label: str,
    expected_system: str,
    expected_machine: str,
    current_executable: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    if label not in {"linux", "macos"}:
        raise RuntimeError("Python build evidence label is invalid")
    if not source.is_dir():
        raise RuntimeError("Python build evidence directory is missing")
    for name in CPYTHON_BUILD_FILES:
        if not (source / name).is_file():
            raise RuntimeError(f"Python build evidence is missing {name}")
    metadata = json.loads(
        (source / "build-metadata.json").read_text(encoding="utf-8")
    )
    expected_hash = expected_cpython_source_sha256(repository)
    expected_target = "linux" if expected_system == "Linux" else "macos"
    required = {
        "builder_target": expected_target,
        "python_implementation": "CPython",
        "python_machine": expected_machine,
        "python_system": expected_system,
        "python_version": EXPECTED_PYTHON,
        "source_sha256": expected_hash,
        "source_tarball": "Python-3.12.13.tar.xz",
        "source_url": (
            "https://www.python.org/ftp/python/3.12.13/"
            "Python-3.12.13.tar.xz"
        ),
    }
    for key, value in required.items():
        if metadata.get(key) != value:
            raise RuntimeError(
                f"Python build evidence field {key!r} does not match"
            )
    recorded_executable = Path(str(metadata.get("python_executable", "")))
    if recorded_executable.resolve() != Path(current_executable).resolve():
        raise RuntimeError(
            "validation is not running with the recorded built interpreter"
        )
    source_hash_line = (source / "source.sha256").read_text(
        encoding="utf-8"
    ).strip()
    if source_hash_line != f"{expected_hash}  Python-3.12.13.tar.xz":
        raise RuntimeError("recorded CPython source checksum does not match")
    if (
        (source / "python-version.txt").read_text(encoding="utf-8").strip()
        != f"Python {EXPECTED_PYTHON}"
    ):
        raise RuntimeError("recorded built Python version does not match")
    copied_names = []
    for name in CPYTHON_BUILD_FILES:
        target_name = f"python-{label}-{name}"
        shutil.copyfile(source / name, destination / target_name)
        copied_names.append(target_name)
    return metadata, tuple(copied_names)


def run(
    command: list[str],
    repository: Path,
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=repository,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def checked_stdout(
    command: list[str], repository: Path, *, timeout: int = 300
) -> str:
    completed = run(command, repository, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr}"
        )
    return completed.stdout.strip()


def render_raw_commands(
    commands: Iterable[list[str]],
    repository: Path,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[str, list[subprocess.CompletedProcess[str]]]:
    sections: list[str] = []
    results: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        completed = run(command, repository, environment=environment)
        results.append(completed)
        sections.append(f"$ {' '.join(command)}\n")
        sections.append(completed.stdout)
        if completed.stdout and not completed.stdout.endswith("\n"):
            sections.append("\n")
        sections.append(completed.stderr)
        if completed.stderr and not completed.stderr.endswith("\n"):
            sections.append("\n")
        sections.append(f"[exit {completed.returncode}]\n\n")
    return "".join(sections), results


def tracked_files(repository: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    names = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    for name in names:
        if "\n" in name or "\r" in name:
            raise RuntimeError("tracked filenames containing newlines are unsupported")
    return sorted(names)


def source_tree_manifest(repository: Path) -> str:
    lines = []
    for relative in tracked_files(repository):
        path = repository / relative
        if not path.is_file():
            raise RuntimeError(f"tracked source is not a regular file: {relative}")
        lines.append(f"{sha256_file(path)}  {relative}\n")
    return "".join(lines)


def verify_source_tree_manifest(repository: Path, manifest_path: Path) -> str:
    expected = manifest_path.read_text(encoding="utf-8")
    actual = source_tree_manifest(repository)
    if actual != expected:
        raise RuntimeError("tracked source tree differs from source-tree.sha256")
    return hashlib.sha256(expected.encode("utf-8")).hexdigest()


def git_identity(repository: Path) -> tuple[str, str]:
    commit = checked_stdout(["git", "rev-parse", "HEAD"], repository)
    status = checked_stdout(["git", "status", "--porcelain=v1"], repository)
    if status:
        raise RuntimeError(f"Git working tree is not clean:\n{status}")
    return commit, status


def create_source_identity(repository: Path, evidence: Path) -> dict[str, object]:
    commit, _ = git_identity(repository)
    manifest = source_tree_manifest(repository)
    manifest_path = evidence / "source-tree.sha256"
    manifest_path.write_text(manifest, encoding="utf-8")
    (evidence / "git-commit.txt").write_text(commit + "\n", encoding="utf-8")
    archive = evidence / "repository.tar"
    completed = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--output",
            str(archive),
            "HEAD",
        ],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git archive failed: {completed.stderr}")
    archive_hash = sha256_file(archive)
    (evidence / "repository.sha256").write_text(
        f"{archive_hash}  {archive.name}\n", encoding="utf-8"
    )
    return {
        "git_commit": commit,
        "tracked_file_count": len(tracked_files(repository)),
        "source_tree_manifest_sha256": hashlib.sha256(
            manifest.encode("utf-8")
        ).hexdigest(),
        "repository_archive": archive.name,
        "repository_archive_bytes": archive.stat().st_size,
        "repository_archive_sha256": archive_hash,
    }


def verify_repository_identity(repository: Path, evidence: Path) -> dict[str, object]:
    commit, _ = git_identity(repository)
    expected_commit = (evidence / "git-commit.txt").read_text(
        encoding="utf-8"
    ).strip()
    if commit != expected_commit:
        raise RuntimeError(
            f"Git commit mismatch: source {expected_commit}, target {commit}"
        )
    manifest_hash = verify_source_tree_manifest(
        repository, evidence / "source-tree.sha256"
    )
    archive_line = (evidence / "repository.sha256").read_text(
        encoding="utf-8"
    ).strip()
    expected_archive_hash, archive_name = archive_line.split("  ", 1)
    archive = evidence / archive_name
    actual_archive_hash = sha256_file(archive)
    if actual_archive_hash != expected_archive_hash:
        raise RuntimeError("repository archive SHA-256 mismatch")
    return {
        "git_commit": commit,
        "source_tree_manifest_sha256": manifest_hash,
        "repository_archive": archive_name,
        "repository_archive_sha256": actual_archive_hash,
    }


def run_full_tests(
    repository: Path, output: Path, environment: dict[str, str]
) -> None:
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    ]
    completed = run(command, repository, environment=environment, timeout=600)
    rendered = (
        f"$ {' '.join(command)}\n"
        + completed.stdout
        + completed.stderr
        + f"[exit {completed.returncode}]\n"
    )
    output.write_text(rendered, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"full test suite failed; see {output.name}")


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def all_evidence_files(
    directory: Path, *, exclude: Iterable[str] = ()
) -> tuple[str, ...]:
    excluded = set(exclude)
    names = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(
                f"symlinks are forbidden in evidence: {path.relative_to(directory)}"
            )
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            if name not in excluded:
                names.append(name)
    return tuple(sorted(names))


def _safe_evidence_path(directory: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RuntimeError(f"unsafe evidence filename: {name!r}")
    path = directory.joinpath(*pure.parts)
    if path.is_symlink():
        raise RuntimeError(f"symlinks are forbidden in evidence: {name}")
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise RuntimeError(f"evidence path escapes its directory: {name}") from exc
    return path


def write_file_manifest(
    directory: Path, names: Iterable[str], destination: str
) -> str:
    lines = []
    unique_names = sorted(set(names))
    if destination in unique_names:
        raise RuntimeError("an evidence manifest cannot include itself")
    _safe_evidence_path(directory, destination)
    for name in unique_names:
        path = _safe_evidence_path(directory, name)
        if not path.is_file():
            raise RuntimeError(f"evidence file is missing: {name}")
        lines.append(f"{sha256_file(path)}  {name}\n")
    content = "".join(lines)
    (directory / destination).write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def verify_file_manifest(directory: Path, manifest_name: str) -> str:
    manifest_path = directory / manifest_name
    content = manifest_path.read_text(encoding="utf-8")
    seen: set[str] = set()
    for line in content.splitlines():
        try:
            expected, name = line.split("  ", 1)
        except ValueError as exc:
            raise RuntimeError(f"invalid evidence manifest line: {line!r}") from exc
        if (
            len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise RuntimeError(f"invalid SHA-256 in evidence manifest: {line!r}")
        if name in seen:
            raise RuntimeError(f"unsafe evidence manifest filename: {name!r}")
        seen.add(name)
        path = _safe_evidence_path(directory, name)
        if not path.is_file() or sha256_file(path) != expected:
            raise RuntimeError(f"evidence file hash mismatch: {name}")
    if not seen:
        raise RuntimeError("evidence manifest is empty")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_failure(evidence: Path, phase: str, exception: BaseException) -> None:
    import traceback

    evidence.mkdir(parents=True, exist_ok=True)
    write_json(
        evidence / "failure.json",
        {
            "phase": phase,
            "recorded_at": utc_now(),
            "error_type": type(exception).__name__,
            "error": str(exception),
            "traceback": traceback.format_exc(),
        },
    )


def fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
