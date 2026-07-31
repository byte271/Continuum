"""Deterministic checkpoint synchronization for harnesses.

Demonstrations, tests, and the cross-platform proof generator all need to
freeze a source process at a known point. Observing program output and then
racing `continuum freeze` against the workload is not synchronization: on a
fast host the workload can finish first, which produced two intermittent
failures in v0.3.0.

This module holds the source at a chosen *execution position* instead. The
hold is a safe-point index, so the checkpoint lands in the same place on every
host regardless of speed, with no sleeps, no enlarged workloads, and no output
markers.

It is private and inert. Nothing activates unless `CONTINUUM_HARNESS_SYNC`
names an existing directory, which only a harness sets. `continuum run` is
untouched otherwise, and the freeze protocol itself is never modified: the
held process still observes a genuinely published request through the ordinary
session mechanism.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import ContinuumError
from .session import read_published_json

SYNC_ENV = "CONTINUUM_HARNESS_SYNC"
HOLD_SAFE_POINT_ENV = "CONTINUUM_HARNESS_HOLD_SAFE_POINT"

# Measured against examples/demo.py: its 5,000-entry build loop ends at safe
# point 15,025, which is also where the first progress line is written, and a
# minimum 1,000-iteration workload ends near safe point 22,375. Holding at
# 16,000 is therefore always after real work and always before the end of the
# shortest accepted workload.
DEFAULT_HOLD_SAFE_POINT = 16_000

READY_TIMEOUT_SECONDS = 120.0
START_TIMEOUT_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.005


class HoldGate:
    """Hold a source process at one safe point until released.

    Publishes a readiness document when the VM reaches `hold_safe_point`, then
    blocks that safe point until the controller creates the start file. The
    controller only creates it after the real freeze request exists on disk,
    so the source cannot run to completion first. Every safe point, including
    the held one, still runs the unmodified session callback.
    """

    def __init__(self, sync_dir: Path, hold_safe_point: int, controller: Any) -> None:
        self.sync_dir = sync_dir
        self.hold_safe_point = hold_safe_point
        self.controller = controller
        self.ready_path = sync_dir / "ready.json"
        self.start_path = sync_dir / "start"
        self.released = False

    def __call__(self, vm: Any) -> None:
        if not self.released and vm.safe_points_executed >= self.hold_safe_point:
            self._signal_ready(vm)
            self._await_start()
            self.released = True
        self.controller.on_safe_point(vm)

    def _signal_ready(self, vm: Any) -> None:
        payload = {
            "session_id": self.controller.session_id,
            "pid": os.getpid(),
            "request_path": str(self.controller.request_path),
            "safe_points_executed": vm.safe_points_executed,
            "instructions_executed": vm.instructions_executed,
        }
        temporary = self.ready_path.with_name(
            f".{self.ready_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.ready_path)
        except OSError as exc:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise ContinuumError(
                f"cannot publish harness readiness to {self.ready_path}: {exc}"
            ) from exc

    def _await_start(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.start_path.exists():
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise ContinuumError(
            f"harness hold was not released within {START_TIMEOUT_SECONDS:.0f}s; "
            f"expected the controller to create {self.start_path}"
        )


def safe_point_callback(controller: Any):
    """Return the controller callback, wrapped only when a harness asked."""

    sync = os.environ.get(SYNC_ENV)
    if not sync:
        return controller.on_safe_point
    sync_dir = Path(sync)
    if not sync_dir.is_dir():
        raise ContinuumError(f"{SYNC_ENV} is not an existing directory: {sync_dir}")
    raw = os.environ.get(HOLD_SAFE_POINT_ENV, "")
    try:
        hold_safe_point = int(raw)
    except ValueError as exc:
        raise ContinuumError(
            f"{HOLD_SAFE_POINT_ENV} must be an integer, got {raw!r}"
        ) from exc
    if hold_safe_point < 1:
        raise ContinuumError(
            f"{HOLD_SAFE_POINT_ENV} must be at least 1, got {hold_safe_point}"
        )
    return HoldGate(sync_dir, hold_safe_point, controller)


# ---------------------------------------------------------------------------
# Controller side. One implementation, used by the demo, the tests, and the
# cross-platform proof generator.
# ---------------------------------------------------------------------------


def environment_for(sync_dir: Path, base: dict[str, str] | None = None,
                    hold_safe_point: int = DEFAULT_HOLD_SAFE_POINT) -> dict[str, str]:
    """Environment that activates the hold for one source process only."""

    environment = dict(base if base is not None else os.environ)
    environment[SYNC_ENV] = str(sync_dir)
    environment[HOLD_SAFE_POINT_ENV] = str(hold_safe_point)
    return environment


def wait_for_ready(process: subprocess.Popen, sync_dir: Path,
                   timeout: float = READY_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Wait until the held source publishes its readiness document."""

    ready_path = sync_dir / "ready.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_path.exists():
            return read_published_json(ready_path, timeout=timeout)
        if process.poll() is not None:
            raise ContinuumError(
                "source exited before reaching its hold safe point "
                f"(status {process.returncode})"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ContinuumError(
        f"source did not reach its hold safe point within {timeout:.0f}s"
    )


def wait_for_request(request_path: Path, source: subprocess.Popen,
                     freeze: subprocess.Popen | None = None,
                     timeout: float = REQUEST_TIMEOUT_SECONDS) -> bool:
    """Wait until the real freeze client has published its request."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if request_path.exists():
            return True
        if freeze is not None and freeze.poll() is not None:
            raise ContinuumError("freeze client exited before publishing a request")
        if source.poll() is not None:
            raise ContinuumError("source exited before the freeze request appeared")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ContinuumError(f"freeze request {request_path} was not published in time")


def freeze_held_source(
    command: list[str],
    session_id: str,
    image: Path,
    source: subprocess.Popen,
    sync_dir: Path,
    ready: dict[str, Any],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Freeze a held source, in the one order that cannot lose the race.

    Starts the real freeze client, waits for its request document to exist
    while the source is still alive, releases the hold only then, and reports
    what was observed. Every caller that freezes a held source uses this, so
    the ordering cannot drift between the demo, the tests, and the proof
    generator.

    Returns evidence describing what was observed, for callers that retain it.
    """

    request_path = Path(ready["request_path"])
    freeze = subprocess.Popen(
        [*command, "freeze", session_id, "-o", str(image)],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_request(request_path, source, freeze)
        source_alive = source.poll() is None
        if not source_alive:
            raise ContinuumError(
                "source exited before the freeze request was published"
            )
        release(sync_dir)
        stdout, stderr = freeze.communicate(timeout=timeout)
    except BaseException:
        if freeze.poll() is None:
            freeze.kill()
            freeze.communicate(timeout=30)
        raise
    return {
        "returncode": freeze.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "safe_points_at_hold": ready["safe_points_executed"],
        "instructions_at_hold": ready["instructions_executed"],
        "source_pid_at_ready": ready["pid"],
        "readiness_published_before_freeze_client": True,
        "request_published_before_release": True,
        "source_alive_when_request_published": source_alive,
        "synchronization": "safe-point hold, not an output marker",
    }


def release(sync_dir: Path) -> None:
    """Release the held safe point. Only valid once."""

    start_path = sync_dir / "start"
    try:
        descriptor = os.open(start_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContinuumError(f"harness hold {start_path} was already released") from exc
    except OSError as exc:
        raise ContinuumError(f"cannot release harness hold {start_path}: {exc}") from exc
    os.close(descriptor)
