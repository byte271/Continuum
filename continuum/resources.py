from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, TextIO

from .errors import ResourceError


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_portable_absolute_path(value: str) -> bool:
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    )


@dataclass
class PortableFile:
    resource_id: str
    original_path: str
    mode: str
    encoding: str | None
    errors: str | None
    newline: str | None
    _handle: BinaryIO | TextIO | None
    initial_size: int
    initial_mtime_ns: int
    initial_sha256: str
    bundled: bool = False

    @property
    def closed(self) -> bool:
        return self._handle is None or self._handle.closed

    @property
    def name(self) -> str:
        return self.original_path

    def _require_handle(self) -> BinaryIO | TextIO:
        if self._handle is None:
            raise ResourceError(f"file resource {self.resource_id} is not rebound")
        return self._handle

    def read(self, size: int = -1) -> Any:
        return self._require_handle().read(size)

    def readline(self, size: int = -1) -> Any:
        return self._require_handle().readline(size)

    def readlines(self, hint: int = -1) -> Any:
        return self._require_handle().readlines(hint)

    def tell(self) -> int:
        return self._require_handle().tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._require_handle().seek(offset, whence)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()

    def snapshot(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": "regular_file",
            "original_path": self.original_path,
            "mode": self.mode,
            "encoding": self.encoding,
            "errors": self.errors,
            "newline": self.newline,
            "offset": None if self.closed else self.tell(),
            "closed": self.closed,
            "size": self.initial_size,
            "mtime_ns": self.initial_mtime_ns,
            "sha256": self.initial_sha256,
            "bundled": self.bundled,
        }


class ResourceManager:
    def __init__(self, policy: str = "strict"):
        if policy not in {"strict", "bundle"}:
            raise ResourceError(f"invalid capture policy: {policy}")
        self.policy = policy
        self.files: dict[str, PortableFile] = {}

    def open_file(
        self,
        path: str | os.PathLike[str],
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> PortableFile:
        if any(marker in mode for marker in ("w", "a", "x", "+")):
            raise ResourceError(
                "Continuum checkpoints only read-only regular files; "
                f"mode {mode!r} is unsupported"
            )
        if mode not in {"r", "rt", "rb"}:
            raise ResourceError(f"unsupported file mode: {mode!r}")
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ResourceError(f"not a regular file: {resolved}")
        stat = resolved.stat()
        text_mode = "b" not in mode
        chosen_encoding = (encoding or "utf-8") if text_mode else None
        handle = open(
            resolved,
            mode,
            buffering=buffering,
            encoding=chosen_encoding,
            errors=errors,
            newline=newline,
        )
        resource_id = f"file-{uuid.uuid4().hex}"
        resource = PortableFile(
            resource_id=resource_id,
            original_path=str(resolved),
            mode=mode,
            encoding=chosen_encoding,
            errors=errors,
            newline=newline,
            _handle=handle,
            initial_size=stat.st_size,
            initial_mtime_ns=stat.st_mtime_ns,
            initial_sha256=_sha256_file(resolved),
            bundled=self.policy == "bundle",
        )
        self.files[resource_id] = resource
        return resource

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        metadata: list[dict[str, Any]] = []
        bundles: dict[str, bytes] = {}
        for resource_id in sorted(self.files):
            resource = self.files[resource_id]
            record = resource.snapshot()
            metadata.append(record)
            if record["bundled"]:
                try:
                    content = Path(resource.original_path).read_bytes()
                except OSError as exc:
                    raise ResourceError(
                        f"cannot bundle {resource.original_path}: {exc}"
                    ) from exc
                if hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise ResourceError(
                        f"file changed while running: {resource.original_path}"
                    )
                bundles[resource_id] = content
        return metadata, bundles

    @classmethod
    def restore(
        cls,
        metadata: list[dict[str, Any]],
        bundles: dict[str, bytes],
        policy: str,
        relocations: dict[str, str] | None = None,
    ) -> tuple["ResourceManager", dict[str, PortableFile]]:
        if policy not in {"strict", "relocate", "bundle"}:
            raise ResourceError(f"invalid restore policy: {policy}")
        relocations = relocations or {}
        manager = cls("bundle" if policy == "bundle" else "strict")
        restored: dict[str, PortableFile] = {}
        try:
            for record in metadata:
                cls._validate_record(record)
                resource_id = record["resource_id"]
                if resource_id in restored:
                    raise ResourceError(
                        f"duplicate resource identifier: {resource_id}"
                    )
                if record["closed"]:
                    resource = PortableFile(
                        resource_id,
                        record["original_path"],
                        record["mode"],
                        record["encoding"],
                        record["errors"],
                        record["newline"],
                        None,
                        record["size"],
                        record["mtime_ns"],
                        record["sha256"],
                        record["bundled"],
                    )
                elif policy == "bundle":
                    content = bundles.get(resource_id)
                    if content is None:
                        raise ResourceError(
                            f"resource {resource_id} was not bundled in this image"
                        )
                    if hashlib.sha256(content).hexdigest() != record["sha256"]:
                        raise ResourceError(f"bundled resource {resource_id} is corrupt")
                    raw = io.BytesIO(content)
                    if "b" in record["mode"]:
                        handle: BinaryIO | TextIO = raw
                    else:
                        handle = io.TextIOWrapper(
                            raw,
                            encoding=record["encoding"] or "utf-8",
                            errors=record["errors"],
                            newline=record["newline"],
                        )
                    handle.seek(record["offset"])
                    resource = PortableFile(
                        resource_id,
                        record["original_path"],
                        record["mode"],
                        record["encoding"],
                        record["errors"],
                        record["newline"],
                        handle,
                        record["size"],
                        record["mtime_ns"],
                        record["sha256"],
                        True,
                    )
                else:
                    original = record["original_path"]
                    candidate = Path(
                        relocations.get(original, original)
                    ).expanduser().resolve()
                    if not candidate.is_file():
                        raise ResourceError(
                            f"required file is missing: {candidate} "
                            f"(originally {original})"
                        )
                    stat = candidate.stat()
                    actual_hash = _sha256_file(candidate)
                    if (
                        stat.st_size != record["size"]
                        or actual_hash != record["sha256"]
                    ):
                        raise ResourceError(
                            f"file identity mismatch for {candidate}: "
                            "size or SHA-256 differs"
                        )
                    if policy == "strict" and stat.st_mtime_ns != record["mtime_ns"]:
                        raise ResourceError(
                            f"strict file identity mismatch for {candidate}: mtime differs"
                        )
                    handle = open(
                        candidate,
                        record["mode"],
                        encoding=(
                            record["encoding"]
                            if "b" not in record["mode"]
                            else None
                        ),
                        errors=record["errors"],
                        newline=record["newline"],
                    )
                    handle.seek(record["offset"])
                    resource = PortableFile(
                        resource_id,
                        str(candidate),
                        record["mode"],
                        record["encoding"],
                        record["errors"],
                        record["newline"],
                        handle,
                        record["size"],
                        record["mtime_ns"],
                        record["sha256"],
                        False,
                    )
                manager.files[resource_id] = resource
                restored[resource_id] = resource
        except BaseException:
            for resource in manager.files.values():
                resource.close()
            raise
        return manager, restored

    @staticmethod
    def _validate_record(record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise ResourceError("resource record is not an object")
        required = {
            "resource_id",
            "kind",
            "original_path",
            "mode",
            "encoding",
            "errors",
            "newline",
            "offset",
            "closed",
            "size",
            "mtime_ns",
            "sha256",
            "bundled",
        }
        missing = required - set(record)
        if missing:
            raise ResourceError(f"resource record is missing: {sorted(missing)}")
        if record["kind"] != "regular_file":
            raise ResourceError(f"unsupported resource kind: {record['kind']!r}")
        if (
            not isinstance(record["resource_id"], str)
            or not record["resource_id"]
            or "/" in record["resource_id"]
            or "\\" in record["resource_id"]
        ):
            raise ResourceError("invalid resource identifier")
        if (
            not isinstance(record["original_path"], str)
            or "\x00" in record["original_path"]
            or not is_portable_absolute_path(record["original_path"])
        ):
            raise ResourceError("invalid original file path")
        if record["mode"] not in {"r", "rt", "rb"}:
            raise ResourceError("invalid resource file mode")
        for field in ("encoding", "errors", "newline"):
            if record[field] is not None and not isinstance(record[field], str):
                raise ResourceError(f"invalid resource {field}")
        if type(record["closed"]) is not bool or type(record["bundled"]) is not bool:
            raise ResourceError("invalid resource boolean")
        if (
            not isinstance(record["size"], int)
            or isinstance(record["size"], bool)
            or record["size"] < 0
            or not isinstance(record["mtime_ns"], int)
            or isinstance(record["mtime_ns"], bool)
        ):
            raise ResourceError("invalid resource file identity")
        if (
            not isinstance(record["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
        ):
            raise ResourceError("invalid resource SHA-256")
        if record["closed"]:
            if record["offset"] is not None:
                raise ResourceError("closed file has an offset")
        elif (
            not isinstance(record["offset"], int)
            or isinstance(record["offset"], bool)
            or record["offset"] < 0
        ):
            raise ResourceError("invalid file offset")
