from __future__ import annotations

import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ContinuumError, FrozenExecution
from .image import save_image

def continuum_home() -> Path:
    configured = os.environ.get("CONTINUUM_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".continuum").resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(descriptor)
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A same-directory hard link publishes the fully written request
            # atomically while preserving O_EXCL behavior for the final name.
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ContinuumError("a freeze request is already pending") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class SessionController:
    def __init__(self, source: str, program: str):
        self.source = source
        self.program = program
        self.session_id = f"cont-{uuid.uuid4().hex[:12]}"
        self.home = continuum_home()
        self.sessions_dir = self.home / "sessions"
        self.requests_dir = self.home / "requests"
        self.responses_dir = self.home / "responses"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sessions_dir, 0o700)
        os.chmod(self.requests_dir, 0o700)
        os.chmod(self.responses_dir, 0o700)
        self.record_path = self.sessions_dir / f"{self.session_id}.json"
        self.request_path = self.requests_dir / f"{self.session_id}.json"
        self.response_path = self.responses_dir / f"{self.session_id}.json"
        self.control_token = uuid.uuid4().hex
        self.freeze_requested = False
        self._previous_signal_handler: Any = None
        self.record = {
            "session_id": self.session_id,
            "pid": os.getpid(),
            "program": program,
            "status": "starting",
            "started_at": _utc_now(),
            "updated_at": _utc_now(),
            "request_path": str(self.request_path),
            "response_path": str(self.response_path),
            "control_token": self.control_token,
            "image": None,
            "error": None,
        }

    def start(self) -> None:
        try:
            self._previous_signal_handler = signal.signal(
                signal.SIGUSR1,
                self._handle_freeze_signal,
            )
        except (ValueError, OSError) as exc:
            raise ContinuumError(
                "Continuum sessions must start on the main POSIX thread"
            ) from exc
        self.record["status"] = "running"
        self._write_record()

    def on_safe_point(self, vm: Any) -> None:
        if not self.freeze_requested:
            return
        self.freeze_requested = False
        if not self.request_path.exists() or self.response_path.exists():
            return
        try:
            request = json.loads(self.request_path.read_text(encoding="utf-8"))
            if request.get("control_token") != self.control_token:
                raise ContinuumError("invalid session control token")
            if request.get("command") != "freeze":
                raise ContinuumError("unknown session command")
            output_path = request.get("output_path")
            if not isinstance(output_path, str) or not output_path:
                raise ContinuumError("freeze request has no output path")
            manifest = save_image(output_path, vm, self.source)
            response = {
                "ok": True,
                "image": output_path,
                "resume_location": manifest["resume_location"],
            }
            _atomic_json(self.response_path, response)
            self.record.update(
                {
                    "status": "frozen",
                    "updated_at": _utc_now(),
                    "image": output_path,
                }
            )
            self._write_record()
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
            _atomic_json(self.response_path, response)
            self.record.update(
                {
                    "status": "running",
                    "updated_at": _utc_now(),
                    "error": f"freeze failed: {exc}",
                }
            )
            self._write_record()
        if response["ok"]:
            raise FrozenExecution

    def finish(self, status: str, error: str | None = None) -> None:
        self.record.update(
            {"status": status, "updated_at": _utc_now(), "error": error}
        )
        self._write_record()
        if self.request_path.exists() and not self.response_path.exists():
            _atomic_json(
                self.response_path,
                {
                    "ok": False,
                    "error": f"session ended with status {status} before a safe point",
                },
            )
        if self._previous_signal_handler is not None:
            signal.signal(signal.SIGUSR1, self._previous_signal_handler)
            self._previous_signal_handler = None

    def _handle_freeze_signal(self, signum: int, frame: Any) -> None:
        if signum == signal.SIGUSR1:
            self.freeze_requested = True

    def _write_record(self) -> None:
        _atomic_json(self.record_path, self.record)


def list_sessions() -> list[dict[str, Any]]:
    directory = continuum_home() / "sessions"
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("cont-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("status") in {"running", "starting"}:
            pid = record.get("pid")
            if not isinstance(pid, int) or not _pid_exists(pid):
                record["status"] = "stale"
        result.append(record)
    return result


def request_freeze(session_id: str, output_path: str) -> dict[str, Any]:
    record_path = continuum_home() / "sessions" / f"{session_id}.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContinuumError(f"unknown session: {session_id}") from exc
    except json.JSONDecodeError as exc:
        raise ContinuumError(f"corrupt session record: {session_id}") from exc
    if record.get("status") != "running":
        raise ContinuumError(
            f"session {session_id} is not running (status: {record.get('status')})"
        )
    request_path_value = record.get("request_path")
    response_path_value = record.get("response_path")
    token = record.get("control_token")
    if (
        not isinstance(request_path_value, str)
        or not isinstance(response_path_value, str)
        or not isinstance(token, str)
    ):
        raise ContinuumError("session has no valid control files")
    request_path = Path(request_path_value)
    response_path = Path(response_path_value)
    payload = {
        "command": "freeze",
        "control_token": token,
        "output_path": str(Path(output_path).expanduser().resolve()),
    }
    _create_json_exclusive(request_path, payload)
    try:
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ContinuumError("session has no valid process identifier")
        try:
            os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            raise ContinuumError(
                f"cannot notify session process {pid}: {exc}"
            ) from exc
        deadline = time.monotonic() + 30
        response = None
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ContinuumError("invalid freeze response") from exc
                break
            try:
                current = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
            if current.get("status") not in {"running", "starting"}:
                raise ContinuumError(
                    f"session ended before freezing (status: {current.get('status')})"
                )
            time.sleep(0.01)
        if response is None:
            raise ContinuumError(
                f"timed out waiting for session {session_id} to freeze"
            )
        if not response.get("ok"):
            raise ContinuumError(response.get("error", "freeze failed"))
        return response
    finally:
        # A failed request must not permanently block a later checkpoint. A
        # timeout also cancels the request so the source cannot freeze after
        # the requesting command already reported failure.
        for control_path in (response_path, request_path):
            try:
                control_path.unlink()
            except FileNotFoundError:
                pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
