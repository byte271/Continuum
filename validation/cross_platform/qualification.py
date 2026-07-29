from __future__ import annotations

from collections.abc import Mapping, Sequence


GITHUB_METADATA_KEYS = (
    "GITHUB_ACTIONS",
    "GITHUB_JOB",
    "GITHUB_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_SERVER_URL",
    "GITHUB_SHA",
    "GITHUB_WORKFLOW",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_WORKFLOW_SHA",
    "ImageOS",
    "ImageVersion",
    "RUNNER_ARCH",
    "RUNNER_ENVIRONMENT",
    "RUNNER_NAME",
    "RUNNER_OS",
)


def github_metadata(environment: Mapping[str, str]) -> dict[str, str]:
    metadata = {key: environment.get(key, "") for key in GITHUB_METADATA_KEYS}
    if (
        metadata["GITHUB_SERVER_URL"]
        and metadata["GITHUB_REPOSITORY"]
        and metadata["GITHUB_RUN_ID"]
    ):
        metadata["WORKFLOW_RUN_URL"] = (
            f"{metadata['GITHUB_SERVER_URL']}/"
            f"{metadata['GITHUB_REPOSITORY']}/actions/runs/"
            f"{metadata['GITHUB_RUN_ID']}"
        )
    else:
        metadata["WORKFLOW_RUN_URL"] = ""
    return metadata


def _github_failures(
    metadata: Mapping[str, str],
    *,
    job: str,
    os_name: str,
    architecture: str,
    git_commit: str,
) -> list[str]:
    failures = []
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": job,
        "GITHUB_SHA": git_commit,
        "GITHUB_WORKFLOW_SHA": git_commit,
        "RUNNER_ARCH": architecture,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": os_name,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            failures.append(f"{key} must be {value!r}")
    for key in (
        "GITHUB_REF",
        "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL",
        "GITHUB_WORKFLOW",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_WORKFLOW_SHA",
        "ImageOS",
        "ImageVersion",
        "RUNNER_NAME",
    ):
        if not metadata.get(key):
            failures.append(f"{key} is missing")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not metadata.get(key, "").isdigit():
            failures.append(f"{key} must be a decimal integer")
    return failures


def qualify_linux_source(
    *,
    system: str,
    machine: str,
    uname_machine: str,
    python_version: str,
    python_system: str,
    python_machine: str,
    python_build_verified: bool,
    hardware_probes_verified: bool,
    container_markers: Sequence[str],
    emulation_markers: Sequence[str],
    git_commit: str,
    environment: Mapping[str, str],
) -> tuple[bool, list[str], dict[str, str]]:
    metadata = github_metadata(environment)
    failures = _github_failures(
        metadata,
        job="linux-source",
        os_name="Linux",
        architecture="X64",
        git_commit=git_commit,
    )
    if system != "Linux":
        failures.append("platform.system() must be 'Linux'")
    if machine != "x86_64":
        failures.append("platform.machine() must be exactly 'x86_64'")
    if uname_machine != "x86_64":
        failures.append("uname -m must be exactly 'x86_64'")
    if python_version != "3.12.13":
        failures.append("Python must be exactly 3.12.13")
    if python_system != "Linux" or python_machine != "x86_64":
        failures.append("the built Python must report native Linux x86_64")
    if not python_build_verified:
        failures.append("the pinned CPython source build is not verified")
    if not hardware_probes_verified:
        failures.append("required Linux hardware probes did not complete")
    if container_markers:
        failures.append("application-container markers were detected")
    if emulation_markers:
        failures.append("known CPU-emulation markers were detected")
    if not metadata.get("ImageOS", "").lower().startswith("ubuntu24"):
        failures.append("ImageOS does not identify an Ubuntu 24 runner image")
    return not failures, failures, metadata


def qualify_macos_target(
    *,
    system: str,
    machine: str,
    uname_machine: str,
    arch: str,
    cpu_brand: str,
    rosetta_translated: bool,
    python_version: str,
    python_system: str,
    python_machine: str,
    python_build_verified: bool,
    git_commit: str,
    environment: Mapping[str, str],
) -> tuple[bool, list[str], dict[str, str]]:
    metadata = github_metadata(environment)
    failures = _github_failures(
        metadata,
        job="macos-target",
        os_name="macOS",
        architecture="ARM64",
        git_commit=git_commit,
    )
    if system != "Darwin":
        failures.append("platform.system() must be 'Darwin'")
    if machine != "arm64":
        failures.append("platform.machine() must be exactly 'arm64'")
    if uname_machine != "arm64":
        failures.append("uname -m must be exactly 'arm64'")
    if arch != "arm64":
        failures.append("arch must be exactly 'arm64'")
    if "apple" not in cpu_brand.lower():
        failures.append("CPU brand does not identify Apple Silicon")
    if rosetta_translated:
        failures.append("the target Python is running through Rosetta")
    if python_version != "3.12.13":
        failures.append("Python must be exactly 3.12.13")
    if python_system != "Darwin" or python_machine != "arm64":
        failures.append("the built Python must report native Darwin arm64")
    if not python_build_verified:
        failures.append("the pinned CPython source build is not verified")
    if not metadata.get("ImageOS", "").lower().startswith("macos26"):
        failures.append("ImageOS does not identify a macOS 26 runner image")
    return not failures, failures, metadata
