"""Experimental execution-ABI images that can cross supported Python versions.

This module deliberately lives beside the shipping image path while the cross-version
contract is being proven. It reuses Continuum's existing container, graph codec, IR,
and frame model, but changes the compatibility decision from exact host/runtime
identity to an explicit execution ABI plus an allowlist of verified Python versions.
"""

from __future__ import annotations

import copy
import json
import os
import platform
import random
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .codec import decode_graph
from .errors import ImageError
from .image import (
    LoadedImage,
    SUPPORTED_CAPABILITIES,
    _frame_metadata,
    _normalized_architecture,
    _parse_json,
    _runtime_python,
    _validate_archive_structure,
    _validate_documents,
    _verify_checksums,
    save_image,
)
from .resources import ResourceManager
from .vm import VirtualMachine

EXECUTION_ABI_VERSION = "1.0"
EXECUTION_ABI_CAPABILITY = f"continuum-execution-abi-{EXECUTION_ABI_VERSION}"
SUPPORTED_PYTHON_VERSIONS = ("3.12.13", "3.13.14")
PORTABLE_SUPPORTED_CAPABILITIES = set(SUPPORTED_CAPABILITIES) | {
    EXECUTION_ABI_CAPABILITY
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _read_archive(path: Path) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImageError(f"not a valid Continuum image: {exc}") from exc
    try:
        with archive:
            infos = archive.infolist()
            _validate_archive_structure(infos)
            return {info.filename: archive.read(info) for info in infos}
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ImageError(f"cannot read Continuum image: {exc}") from exc


def _write_archive(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        strict_timestamps=True,
    ) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _rehash(entries: dict[str, bytes]) -> None:
    covered = {
        name: _sha256(content)
        for name, content in sorted(entries.items())
        if name not in {"checksums.json", "SIGNATURE"}
    }
    entries["checksums.json"] = _json_bytes(
        {"algorithm": "sha256", "entries": covered}
    )


def save_portable_image(
    path: str | os.PathLike[str],
    vm: VirtualMachine,
    source: str,
    source_os: str | None = None,
    source_architecture: str | None = None,
) -> dict[str, Any]:
    """Write an image whose restore contract is the execution ABI, not CPython.

    The ordinary writer still performs every existing preflight and graph round-trip.
    This wrapper then adds a narrowly scoped, checksummed compatibility extension.
    """

    current_python = platform.python_version()
    if current_python not in SUPPORTED_PYTHON_VERSIONS:
        raise ImageError(
            "portable image creation requires one of "
            f"{list(SUPPORTED_PYTHON_VERSIONS)}; current is {current_python}"
        )

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.portable-", dir=destination.parent
    ) as temporary:
        root = Path(temporary)
        base = root / "base.cont"
        completed = root / "portable.cont"
        save_image(
            base,
            vm,
            source,
            source_os=source_os,
            source_architecture=source_architecture,
        )
        entries = _read_archive(base)
        manifest = _parse_json(entries["manifest.json"], "manifest.json")
        runtime = _parse_json(entries["runtime.json"], "runtime.json")
        compatibility = manifest["target_compatibility"]

        capabilities = set(compatibility["required_capabilities"])
        capabilities.add(EXECUTION_ABI_CAPABILITY)
        compatibility["required_capabilities"] = sorted(capabilities)
        compatibility["execution_abi"] = EXECUTION_ABI_VERSION
        compatibility["python_versions"] = list(SUPPORTED_PYTHON_VERSIONS)
        compatibility["runtime_version_policy"] = "execution-abi"
        # Kept as creator provenance and for strict readers. Portable readers use
        # python_versions plus execution_abi for the target decision.
        compatibility["python_version"] = current_python
        compatibility["runtime_version"] = __version__
        runtime["execution_abi"] = EXECUTION_ABI_VERSION
        runtime["runtime_version_policy"] = "execution-abi"
        manifest["portable_across_python_versions"] = True

        entries["manifest.json"] = _json_bytes(manifest)
        entries["runtime.json"] = _json_bytes(runtime)
        _rehash(entries)
        _write_archive(completed, entries)
        os.replace(completed, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    return manifest


class PortableLoadedImage(LoadedImage):
    """Loaded image that validates against a stable execution ABI."""

    def validate_compatibility(self) -> None:
        compatibility = self.manifest["target_compatibility"]
        if compatibility.get("runtime_implementation") != "continuum-vm":
            raise ImageError("image requires an unsupported runtime implementation")
        if compatibility.get("native_payload_required") is not False:
            raise ImageError("image requires a native payload")

        capabilities = compatibility.get("required_capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            raise ImageError("image has invalid required capabilities")
        unknown = set(capabilities) - PORTABLE_SUPPORTED_CAPABILITIES
        if unknown:
            raise ImageError(
                f"image requires unknown capabilities: {sorted(unknown)}"
            )
        if EXECUTION_ABI_CAPABILITY not in capabilities:
            raise ImageError("image does not declare the portable execution ABI")
        if compatibility.get("execution_abi") != EXECUTION_ABI_VERSION:
            raise ImageError(
                "execution ABI mismatch: image requires "
                f"{compatibility.get('execution_abi')!r}, runtime provides "
                f"{EXECUTION_ABI_VERSION!r}"
            )
        if self.runtime.get("execution_abi") != EXECUTION_ABI_VERSION:
            raise ImageError("runtime metadata has an incompatible execution ABI")
        if compatibility.get("runtime_version_policy") != "execution-abi":
            raise ImageError("image does not use execution-ABI runtime compatibility")

        current_os = platform.system()
        current_arch = _normalized_architecture()
        if current_os not in compatibility["operating_systems"]:
            raise ImageError(f"target operating system is unsupported: {current_os}")
        if current_arch not in compatibility["architectures"]:
            raise ImageError(f"target architecture is unsupported: {current_arch}")
        platforms = compatibility.get("platforms")
        if platforms is not None and {
            "os": current_os,
            "architecture": current_arch,
        } not in platforms:
            raise ImageError(
                f"target platform is unsupported: {current_os} {current_arch}"
            )

        python_versions = compatibility.get("python_versions")
        if (
            not isinstance(python_versions, list)
            or not python_versions
            or any(not isinstance(item, str) for item in python_versions)
            or len(set(python_versions)) != len(python_versions)
        ):
            raise ImageError("image has an invalid target Python version list")
        current_python = _runtime_python()
        if current_python not in python_versions:
            raise ImageError(
                f"Python version {current_python} is not accepted by this image; "
                f"accepted versions are {python_versions}"
            )
        if current_python not in SUPPORTED_PYTHON_VERSIONS:
            raise ImageError(
                f"runtime has not verified Python {current_python}; verified versions "
                f"are {list(SUPPORTED_PYTHON_VERSIONS)}"
            )


def _validate_portable_metadata(
    manifest: dict[str, Any], runtime: dict[str, Any]
) -> None:
    compatibility = manifest.get("target_compatibility")
    source = manifest.get("source")
    if not isinstance(compatibility, dict) or not isinstance(source, dict):
        raise ImageError("invalid portable compatibility metadata")
    python_versions = compatibility.get("python_versions")
    if (
        not isinstance(python_versions, list)
        or not python_versions
        or any(not isinstance(item, str) for item in python_versions)
        or len(set(python_versions)) != len(python_versions)
    ):
        raise ImageError("invalid portable Python version list")
    source_python = source.get("python_version")
    if source_python not in python_versions:
        raise ImageError("source Python is absent from target compatibility")
    if compatibility.get("python_version") != source_python:
        raise ImageError("creator Python provenance is inconsistent")
    if compatibility.get("execution_abi") != EXECUTION_ABI_VERSION:
        raise ImageError("unsupported portable execution ABI")
    if runtime.get("execution_abi") != compatibility.get("execution_abi"):
        raise ImageError("execution ABI metadata is inconsistent")
    if compatibility.get("runtime_version_policy") != "execution-abi":
        raise ImageError("invalid portable runtime version policy")
    if runtime.get("runtime_version_policy") != "execution-abi":
        raise ImageError("runtime omits the portable version policy")
    capabilities = compatibility.get("required_capabilities")
    if not isinstance(capabilities, list) or EXECUTION_ABI_CAPABILITY not in capabilities:
        raise ImageError("portable image omits its mandatory execution ABI capability")
    if runtime.get("runtime_version") != compatibility.get("runtime_version"):
        raise ImageError("creator runtime provenance is inconsistent")


def load_portable_image(path: str | os.PathLike[str]) -> PortableLoadedImage:
    source_path = Path(path).expanduser().resolve()
    raw = _read_archive(source_path)
    checksums = _parse_json(raw["checksums.json"], "checksums.json")
    _verify_checksums(raw, checksums)
    manifest = _parse_json(raw["manifest.json"], "manifest.json")
    runtime = _parse_json(raw["runtime.json"], "runtime.json")
    ir = _parse_json(raw["code/ir.json"], "code/ir.json")
    modules = _parse_json(raw["modules/hashes.json"], "modules/hashes.json")
    heap = _parse_json(raw["heap/objects.json"], "heap/objects.json")
    resources = _parse_json(
        raw["resources/resources.json"], "resources/resources.json"
    )
    frames = _parse_json(raw["frames/frames.json"], "frames/frames.json")

    _validate_portable_metadata(manifest, runtime)
    # Reuse the shipping validator for every existing invariant. Its capability
    # set predates this experiment, so validate a copy with only the new ABI
    # marker removed; checksums were already verified against the original bytes.
    strict_manifest = copy.deepcopy(manifest)
    strict_capabilities = strict_manifest["target_compatibility"][
        "required_capabilities"
    ]
    strict_manifest["target_compatibility"]["required_capabilities"] = [
        item for item in strict_capabilities if item != EXECUTION_ABI_CAPABILITY
    ]
    _validate_documents(
        strict_manifest,
        runtime,
        ir,
        modules,
        heap,
        resources,
        frames,
        raw,
    )

    try:
        source = raw["code/program.py"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImageError("entry program is not UTF-8") from exc
    bundles: dict[str, bytes] = {}
    for record in resources["resources"]:
        name = f"resources/files/{record['resource_id']}.bin"
        if name in raw:
            bundles[record["resource_id"]] = raw[name]
    return PortableLoadedImage(
        manifest,
        runtime,
        ir,
        source,
        heap,
        resources,
        frames,
        bundles,
    )


def verify_portable_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    loaded = load_portable_image(path)
    loaded.validate_compatibility()
    resource_placeholders = {
        record["resource_id"]: object()
        for record in loaded.resources_document["resources"]
    }
    state = decode_graph(loaded.heap, resource_placeholders)
    random_state = random.getstate()
    try:
        vm = VirtualMachine.restore(
            loaded.ir,
            state,
            ResourceManager("strict"),
        )
    finally:
        random.setstate(random_state)
    expected_frames = {"frames": _frame_metadata(vm)}
    if loaded.frames_document != expected_frames:
        raise ImageError("inspectable frame metadata does not match restorable state")
    return {
        "manifest": loaded.manifest,
        "integrity": "verified",
        "compatibility": "accepted",
        "execution_abi": EXECUTION_ABI_VERSION,
        "source_python": loaded.manifest["source"]["python_version"],
        "target_python": _runtime_python(),
        "graph": "verified",
        "frames": "verified",
        "resources": "metadata-verified-not-opened",
    }
