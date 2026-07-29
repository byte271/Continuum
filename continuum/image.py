from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import FORMAT_VERSION, IR_VERSION, SUPPORTED_PYTHON, __version__
from .codec import decode_graph, encode_graph
from .errors import ImageError, ResourceError
from .resources import ResourceManager
from .vm import VirtualMachine, validate_ir

MAX_ENTRIES = 10_000
MAX_ENTRY_SIZE = 128 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
REQUIRED_ENTRIES = {
    "manifest.json",
    "runtime.json",
    "code/program.py",
    "code/ir.json",
    "modules/hashes.json",
    "heap/objects.json",
    "frames/frames.json",
    "resources/resources.json",
    "checksums.json",
}
STATIC_ENTRIES = REQUIRED_ENTRIES | {"SIGNATURE"}
SUPPORTED_CAPABILITIES = {
    f"continuum-ir-{IR_VERSION}",
    "explicit-frames",
    "graph-codec-0.1",
    "portable-readonly-files",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalized_architecture() -> str:
    value = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(value, value)


def _runtime_python() -> str:
    return platform.python_version()


def _frame_metadata(vm: VirtualMachine) -> list[dict[str, Any]]:
    result = []
    for depth, frame in enumerate(vm.frames):
        function = vm.ir["functions"][frame.function_id]
        instruction = function["code"][frame.pc]
        result.append(
            {
                "depth": depth,
                "function_id": frame.function_id,
                "function": function["name"],
                "pc": frame.pc,
                "line": instruction["line"],
                "operand_stack_depth": len(frame.stack),
                "locals": sorted(frame.locals),
                "control_blocks": len(frame.blocks),
                "pending_finally_reasons": len(frame.finally_reasons),
            }
        )
    return result


def save_image(
    path: str | os.PathLike[str],
    vm: VirtualMachine,
    source: str,
    source_os: str | None = None,
    source_architecture: str | None = None,
) -> dict[str, Any]:
    if vm.completed or not vm.frames:
        raise ImageError("cannot freeze a completed execution")
    validate_ir(vm.ir)
    actual_source_hash = _sha256(source.encode("utf-8"))
    if actual_source_hash != vm.ir["source_sha256"]:
        raise ImageError("source text does not match the running IR")

    resource_records, bundle_data = vm.resources.snapshot()
    heap = encode_graph(vm.state_root())
    # Encoding alone is not proof that the graph can be reconstructed. Decode
    # it before creating the destination so the source cannot terminate after
    # committing an image that this runtime already knows is unresumable.
    decode_graph(
        heap,
        dict(vm.resources.files),
    )
    frames = _frame_metadata(vm)
    ir_bytes = _json_bytes(vm.ir)
    source_bytes = source.encode("utf-8")
    heap_bytes = _json_bytes(heap)
    resources_bytes = _json_bytes(
        {
            "policy": vm.resources.policy,
            "resources": resource_records,
        }
    )
    runtime = {
        "runtime_implementation": "continuum-vm",
        "runtime_version": __version__,
        "python_version": _runtime_python(),
        "ir_version": vm.ir["ir_version"],
        "instructions_executed": vm.instructions_executed,
        "safe_points_executed": vm.safe_points_executed,
        "argv": vm.argv,
    }
    module_hashes = {
        "entry_program": actual_source_hash,
        "continuum_ir": _sha256(ir_bytes),
        "stdlib_modules": {
            name: {
                "binding": "exact-python-version-and-allowlisted-name",
                "source_hash": None,
            }
            for name in vm.ir.get("imports", [])
        },
    }
    resume_frame = frames[-1]
    manifest = {
        "format": "Continuum Portable Process Image",
        "format_version": FORMAT_VERSION,
        "created_by": __version__,
        "source": {
            "os": source_os or platform.system(),
            "architecture": source_architecture or _normalized_architecture(),
            "python_version": _runtime_python(),
        },
        "target_compatibility": {
            "operating_systems": ["Linux", "Darwin", "Windows"],
            "architectures": ["x86_64", "arm64"],
            "platforms": [
                {"os": "Linux", "architecture": "x86_64"},
                {"os": "Linux", "architecture": "arm64"},
                {"os": "Darwin", "architecture": "x86_64"},
                {"os": "Darwin", "architecture": "arm64"},
                {"os": "Windows", "architecture": "x86_64"},
            ],
            "python_version": SUPPORTED_PYTHON,
            "runtime_implementation": "continuum-vm",
            "runtime_version": __version__,
            "native_payload_required": False,
            "required_capabilities": sorted(SUPPORTED_CAPABILITIES),
        },
        "entry_program": vm.ir["source_name"],
        "entry_program_sha256": actual_source_hash,
        "module_hashes_entry": "modules/hashes.json",
        "frames": len(frames),
        "heap_objects": len(heap["objects"]),
        "open_files": sum(not item["closed"] for item in resource_records),
        "resume_location": {
            "function": resume_frame["function"],
            "file": vm.ir["source_name"],
            "line": resume_frame["line"],
            "logical_instruction": resume_frame["pc"],
        },
        "supported_resources": [
            {
                "resource_id": item["resource_id"],
                "kind": item["kind"],
                "policy": "bundle" if item["bundled"] else "strict",
            }
            for item in resource_records
        ],
        "unsupported_resources": [],
        "compression": "zip-deflate",
        "integrity": {
            "algorithm": "sha256",
            "checksums_entry": "checksums.json",
            "signature": "not-present",
        },
        "security_boundary": "executable-untrusted-content",
    }

    entries: dict[str, bytes] = {
        "manifest.json": _json_bytes(manifest),
        "runtime.json": _json_bytes(runtime),
        "code/program.py": source_bytes,
        "code/ir.json": ir_bytes,
        "modules/hashes.json": _json_bytes(module_hashes),
        "heap/objects.json": heap_bytes,
        "frames/frames.json": _json_bytes({"frames": frames}),
        "resources/resources.json": resources_bytes,
    }
    for resource_id, content in bundle_data.items():
        entries[f"resources/files/{resource_id}.bin"] = content
    checksums = {
        "algorithm": "sha256",
        "entries": {name: _sha256(content) for name, content in sorted(entries.items())},
    }
    entries["checksums.json"] = _json_bytes(checksums)

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        with zipfile.ZipFile(
            temporary_name,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            strict_timestamps=True,
        ) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
        with open(temporary_name, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return manifest


@dataclass
class LoadedImage:
    manifest: dict[str, Any]
    runtime: dict[str, Any]
    ir: dict[str, Any]
    source: str
    heap: dict[str, Any]
    resources_document: dict[str, Any]
    frames_document: dict[str, Any]
    bundles: dict[str, bytes]

    def restore_vm(
        self,
        policy: str | None = None,
        relocations: dict[str, str] | None = None,
        safe_point_callback: Any = None,
    ) -> VirtualMachine:
        self.validate_compatibility()
        capture_policy = self.resources_document["policy"]
        restore_policy = policy or capture_policy
        manager, resource_map = ResourceManager.restore(
            self.resources_document["resources"],
            self.bundles,
            restore_policy,
            relocations,
        )
        try:
            state = decode_graph(self.heap, resource_map)
            return VirtualMachine.restore(
                self.ir, state, manager, safe_point_callback
            )
        except BaseException:
            for resource in manager.files.values():
                resource.close()
            raise

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
        unknown = set(capabilities) - SUPPORTED_CAPABILITIES
        if unknown:
            raise ImageError(
                f"image requires unknown capabilities: {sorted(unknown)}"
            )
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
        if _runtime_python() != compatibility["python_version"]:
            raise ImageError(
                f"Python version mismatch: image requires "
                f"{compatibility['python_version']}, current runtime is {_runtime_python()}"
            )
        if compatibility["runtime_version"] != __version__:
            raise ImageError(
                f"runtime version mismatch: image requires "
                f"{compatibility['runtime_version']}, installed runtime is {__version__}"
            )


def load_image(path: str | os.PathLike[str]) -> LoadedImage:
    source_path = Path(path).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(source_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImageError(f"not a valid Continuum image: {exc}") from exc
    try:
        with archive:
            infos = archive.infolist()
            _validate_archive_structure(infos)
            raw = {info.filename: archive.read(info) for info in infos}
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ImageError(f"cannot read Continuum image: {exc}") from exc
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
    _validate_documents(
        manifest, runtime, ir, modules, heap, resources, frames, raw
    )
    try:
        source = raw["code/program.py"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImageError("entry program is not UTF-8") from exc
    bundles = {}
    for record in resources["resources"]:
        name = f"resources/files/{record['resource_id']}.bin"
        if name in raw:
            bundles[record["resource_id"]] = raw[name]
    return LoadedImage(
        manifest,
        runtime,
        ir,
        source,
        heap,
        resources,
        frames,
        bundles,
    )


def inspect_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    loaded = load_image(path)
    return {
        "manifest": loaded.manifest,
        "runtime": loaded.runtime,
        "integrity": "verified",
    }


def verify_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    loaded = load_image(path)
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
        raise ImageError(
            "inspectable frame metadata does not match restorable state"
        )
    return {
        "manifest": loaded.manifest,
        "integrity": "verified",
        "compatibility": "accepted",
        "graph": "verified",
        "frames": "verified",
        "resources": "metadata-verified-not-opened",
    }


def _validate_archive_structure(infos: list[zipfile.ZipInfo]) -> None:
    if not infos or len(infos) > MAX_ENTRIES:
        raise ImageError("image has an invalid number of entries")
    names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            name in names
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in name
            or name.endswith("/")
        ):
            raise ImageError(f"unsafe or duplicate image entry: {name!r}")
        names.add(name)
        if info.file_size > MAX_ENTRY_SIZE:
            raise ImageError(f"image entry is too large: {name}")
        total += info.file_size
        if total > MAX_TOTAL_SIZE:
            raise ImageError("image expands beyond the configured size limit")
        if (
            info.compress_size > 0
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise ImageError(f"suspicious compression ratio: {name}")
        if name not in STATIC_ENTRIES and not name.startswith("resources/files/"):
            raise ImageError(f"unexpected image entry: {name}")
    missing = REQUIRED_ENTRIES - names
    if missing:
        raise ImageError(f"image is missing entries: {sorted(missing)}")


def _verify_checksums(raw: dict[str, bytes], document: Any) -> None:
    if (
        not isinstance(document, dict)
        or document.get("algorithm") != "sha256"
        or not isinstance(document.get("entries"), dict)
    ):
        raise ImageError("invalid checksum document")
    expected_names = set(raw) - {"checksums.json", "SIGNATURE"}
    if set(document["entries"]) != expected_names:
        raise ImageError("checksum coverage does not match image entries")
    for name in sorted(expected_names):
        expected = document["entries"][name]
        if not isinstance(expected, str) or _sha256(raw[name]) != expected:
            raise ImageError(f"integrity check failed for {name}")


def _parse_json(content: bytes, name: str) -> Any:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ImageError(f"invalid JSON in {name}") from exc


def _validate_documents(
    manifest: Any,
    runtime: Any,
    ir: Any,
    modules: Any,
    heap: Any,
    resources: Any,
    frames: Any,
    raw: dict[str, bytes],
) -> None:
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise ImageError("unsupported image format version")
    if manifest.get("security_boundary") != "executable-untrusted-content":
        raise ImageError("image omits its executable-content security boundary")
    compatibility = manifest.get("target_compatibility")
    source_metadata = manifest.get("source")
    if not isinstance(compatibility, dict) or not isinstance(source_metadata, dict):
        raise ImageError("invalid compatibility metadata")
    if (
        compatibility.get("runtime_implementation") != "continuum-vm"
        or compatibility.get("native_payload_required") is not False
    ):
        raise ImageError("image requests an unsupported runtime payload")
    capabilities = compatibility.get("required_capabilities")
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) for item in capabilities
    ):
        raise ImageError("invalid required capability list")
    unknown = set(capabilities) - SUPPORTED_CAPABILITIES
    if unknown:
        raise ImageError(f"unknown mandatory image capabilities: {sorted(unknown)}")
    platforms = compatibility.get("platforms")
    if platforms is not None and (
        not isinstance(platforms, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"os", "architecture"}
            or not isinstance(item["os"], str)
            or not isinstance(item["architecture"], str)
            for item in platforms
        )
    ):
        raise ImageError("invalid target platform compatibility list")
    if (
        not isinstance(runtime, dict)
        or runtime.get("runtime_implementation") != "continuum-vm"
    ):
        raise ImageError("invalid runtime metadata")
    validate_ir(ir)
    if _sha256(raw["code/program.py"]) != manifest.get("entry_program_sha256"):
        raise ImageError("program hash does not match manifest")
    if (
        ir.get("source_sha256") != manifest.get("entry_program_sha256")
        or ir.get("source_name") != manifest.get("entry_program")
    ):
        raise ImageError("IR source identity does not match manifest")
    if (
        runtime.get("runtime_version") != compatibility.get("runtime_version")
        or runtime.get("python_version") != source_metadata.get("python_version")
        or runtime.get("python_version") != compatibility.get("python_version")
        or runtime.get("ir_version") != ir.get("ir_version")
    ):
        raise ImageError("runtime metadata is inconsistent")
    if (
        not isinstance(modules, dict)
        or modules.get("entry_program") != manifest.get("entry_program_sha256")
        or modules.get("continuum_ir") != _sha256(raw["code/ir.json"])
    ):
        raise ImageError("module hash document is inconsistent")
    if not isinstance(resources, dict) or resources.get("policy") not in {
        "strict",
        "bundle",
    }:
        raise ImageError("invalid resource metadata")
    if not isinstance(resources.get("resources"), list):
        raise ImageError("invalid resource table")
    resource_ids: set[str] = set()
    expected_bundles: set[str] = set()
    for record in resources["resources"]:
        try:
            ResourceManager._validate_record(record)
        except ResourceError as exc:
            raise ImageError(f"invalid resource metadata: {exc}") from exc
        resource_id = record["resource_id"]
        if resource_id in resource_ids:
            raise ImageError(f"duplicate resource identifier: {resource_id}")
        resource_ids.add(resource_id)
        if record["bundled"]:
            expected_bundles.add(f"resources/files/{resource_id}.bin")
    actual_bundles = {
        name for name in raw if name.startswith("resources/files/")
    }
    if expected_bundles != actual_bundles:
        raise ImageError("bundled resource entries do not match resource metadata")
    if (
        not isinstance(heap, dict)
        or not isinstance(heap.get("objects"), list)
        or len(heap["objects"]) != manifest.get("heap_objects")
    ):
        raise ImageError("heap object count does not match manifest")
    if not isinstance(frames, dict) or not isinstance(frames.get("frames"), list):
        raise ImageError("invalid frame metadata")
    if len(frames["frames"]) != manifest.get("frames"):
        raise ImageError("frame count does not match manifest")
