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

WINDOWS_REQUEST_POLL_INTERVAL_SECONDS = 0.01


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


PUBLISHED_READ_POLL_SECONDS = 0.002
# How long a control document may stay unopenable before the VM gives up on a
# freeze request entirely. Spent across safe points, never inside one.
REQUEST_UNREADABLE_BUDGET_SECONDS = 30.0


class PublicationPending(Exception):
    """A published document exists but cannot be opened yet.

    Windows leaves a short window around an atomic replace or hard link in
    which the final name exists but opening it fails with EACCES; antivirus
    and indexers widen it. This is a transient condition, not corruption, and
    each call site decides how long it is willing to wait for it.
    """


def read_published_json(path: Path, timeout: float = 0.0):
    """Read a document published by atomic replace or hard link.

    One parser and one retry classification for every call site. `timeout` is
    how long *this* call may block: the default of zero never blocks, and
    raises PublicationPending so a caller with its own outer loop or its own
    deadline decides what to do. A malformed document is corruption and raises
    immediately, because atomic publication is never partial. A missing
    document raises FileNotFoundError and is never confused with a pending one.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise PublicationPending(str(path)) from exc
        except json.JSONDecodeError:
            raise
        time.sleep(PUBLISHED_READ_POLL_SECONDS)


def _uses_signal_notifications() -> bool:
    return os.name == "posix" and hasattr(signal, "SIGUSR1")


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
        self._signal_notifications = _uses_signal_notifications()
        self._next_request_poll = 0.0
        # Set when a freeze request exists but could not be opened yet. The
        # request stays pending and is retried at a later safe point; the
        # budget is spent across safe points, never inside one.
        self._request_unreadable_since: float | None = None
        self._previous_signal_handler: Any = None
        self._checkpoints: Any = None
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
        if self._signal_notifications:
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

    def attach_checkpoints(self, scheduler: Any) -> None:
        """Attach a rolling checkpoint scheduler to this session.

        Kept separate from the freeze protocol on purpose: a checkpoint never
        produces a freeze response, never changes session status to `frozen`,
        and never raises FrozenExecution. The two mechanisms share only the
        safe point they observe.
        """

        self._checkpoints = scheduler
        self.record["checkpoint_directory"] = scheduler.status.directory
        self.record["checkpoint_lineage_id"] = scheduler.lineage_id
        self._write_record()

    def on_safe_point(self, vm: Any) -> None:
        checkpoints = getattr(self, "_checkpoints", None)
        if checkpoints is not None:
            # Runs before the freeze check so a pending freeze cannot starve
            # checkpointing, and returns normally so execution continues.
            checkpoints.on_safe_point(vm)
        if not self.freeze_requested:
            if self._signal_notifications:
                return
            now = time.monotonic()
            if now < self._next_request_poll:
                return
            self._next_request_poll = (
                now + WINDOWS_REQUEST_POLL_INTERVAL_SECONDS
            )
            if not self.request_path.exists():
                return
            self.freeze_requested = True
        self.freeze_requested = False
        if not self.request_path.exists() or self.response_path.exists():
            return
        try:
            try:
                # Zero timeout: a safe point must not sleep.
                request = read_published_json(self.request_path)
            except PublicationPending:
                now = time.monotonic()
                if self._request_unreadable_since is None:
                    self._request_unreadable_since = now
                elif (
                    now - self._request_unreadable_since
                    >= REQUEST_UNREADABLE_BUDGET_SECONDS
                ):
                    self._request_unreadable_since = None
                    raise ContinuumError(
                        "freeze request could not be opened within "
                        f"{REQUEST_UNREADABLE_BUDGET_SECONDS:.0f}s: "
                        f"{self.request_path}"
                    )
                # Keep the request pending and let execution continue. The
                # existing Windows poll interval already rate-limits the
                # retry, so this cannot busy-poll.
                self.freeze_requested = True
                return
            self._request_unreadable_since = None
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
        if self._signal_notifications and signum == signal.SIGUSR1:
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
            # Non-blocking: one temporarily unopenable record must not stall
            # `continuum sessions`. It is reported as unreadable instead.
            record = read_published_json(path)
        except PublicationPending:
            result.append(
                {"session_id": path.stem, "status": "unreadable", "pid": None,
                 "program": str(path)}
            )
            continue
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
        record = read_published_json(record_path, timeout=1.0)
    except PublicationPending as exc:
        raise ContinuumError(
            f"session record is not readable: {record_path}"
        ) from exc
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
        if _uses_signal_notifications():
            try:
                os.kill(pid, signal.SIGUSR1)
            except (ProcessLookupError, PermissionError, OSError) as exc:
                raise ContinuumError(
                    f"cannot notify session process {pid}: {exc}"
                ) from exc
        elif not _pid_exists(pid):
            raise ContinuumError(f"session process {pid} is not running")
        deadline = time.monotonic() + 30
        response = None
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = read_published_json(response_path)
                except (FileNotFoundError, PublicationPending):
                    # Not readable yet or cancelled; spend the outer 30s
                    # deadline rather than an independent one.
                    time.sleep(0.01)
                    continue
                except json.JSONDecodeError as exc:
                    raise ContinuumError("invalid freeze response") from exc
                break
            try:
                current = read_published_json(record_path)
            except (OSError, PublicationPending, json.JSONDecodeError):
                # Unreadable right now says nothing about the session; the
                # outer deadline governs.
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
    if os.name == "nt":
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
