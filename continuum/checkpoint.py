"""Rolling crash-recovery checkpoints.

This is deliberately *not* the freeze path. `continuum freeze` commits one image
and terminates the source process; that behaviour is unchanged. A periodic
checkpoint commits an image and lets the same process keep running from the next
logical instruction.

Both paths share one writer (`image.save_image`) and one reader
(`image.load_image`), so a checkpoint is an ordinary inspectable Continuum image
and recovery reuses the audited restore path rather than a second loader.

Concurrency
-----------
There is none, on purpose. Checkpoints run synchronously inside the VM's
safe-point callback, so the VM is stopped at a known-consistent point for the
whole capture. No thread observes mutable VM state while it is being mutated,
there is no worker to leak, no daemon thread that can vanish mid-commit, and no
queue that can grow without bound. The cost is a visible pause, which is
measured and reported separately from the requested interval rather than hidden.

Durability
----------
See `commit`. The short version: contents are flushed before the atomic
replace, and the directory entry is flushed after it where the platform
supports that. Where it does not, the weaker guarantee is recorded in the image
and surfaced by `continuum checkpoints` instead of being silently assumed.
"""

from __future__ import annotations

import errno
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import CheckpointError, ImageError
from .image import (
    CHECKPOINT_METADATA_VERSION,
    CHECKPOINT_MODE_PERIODIC,
    DIRECTORY_FSYNC_SUPPORTED,
    DIRECTORY_FSYNC_UNSUPPORTED,
    load_image,
    save_image,
)

SLOT_NAMES = ("slot-a", "slot-b")
SLOT_SUFFIX = ".cont"
TEMPORARY_PREFIX = ".checkpoint-"
TEMPORARY_SUFFIX = ".tmp"
MIN_INTERVAL_SECONDS = 0.001
MAX_INTERVAL_SECONDS = 86_400.0
MIN_SLOTS = 2
MAX_SLOTS = 8

FAILURE_CONTINUE = "continue"
FAILURE_TERMINATE = "terminate"
FAILURE_POLICIES = (FAILURE_CONTINUE, FAILURE_TERMINATE)

# Commit stages, in the order they occur. Tests inject real failures at these
# boundaries; production runs with no hook installed. The names are part of the
# test contract, so they are defined once here rather than spelled in tests.
STAGE_BEFORE_TEMPORARY = "before-temporary-create"
# Fires with the temporary path chosen but no bytes written yet. A failure
# genuinely *inside* serialization is injected instead by making the VM's state
# unserializable partway through, which produces a real half-written temporary
# file rather than a simulated one.
STAGE_DURING_SERIALIZATION = "during-serialization"
STAGE_AFTER_TEMPORARY_WRITE = "after-temporary-write"
STAGE_AFTER_FLUSH = "after-flush"
STAGE_DURING_RENAME = "during-rename"
STAGE_AFTER_RENAME = "after-rename"
STAGE_AFTER_DIRECTORY_FLUSH = "after-directory-flush"
COMMIT_STAGES = (
    STAGE_BEFORE_TEMPORARY,
    STAGE_DURING_SERIALIZATION,
    STAGE_AFTER_TEMPORARY_WRITE,
    STAGE_AFTER_FLUSH,
    STAGE_DURING_RENAME,
    STAGE_AFTER_RENAME,
    STAGE_AFTER_DIRECTORY_FLUSH,
)

_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m)?$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, None: 1.0}

# Test seam. `None` in production; a callable receives each stage name and may
# raise to simulate a crash at that exact point. It is never used to fake
# success -- injected failures produce real partial on-disk state, which the
# recovery tests then read back through the ordinary reader.
_commit_hook: Callable[[str], None] | None = None


def set_commit_hook(hook: Callable[[str], None] | None) -> Callable[[str], None] | None:
    """Install a commit-stage hook and return the previous one."""

    global _commit_hook
    previous = _commit_hook
    _commit_hook = hook
    return previous


def _stage(name: str) -> None:
    if _commit_hook is not None:
        _commit_hook(name)


def parse_interval(text: str) -> float:
    """Parse a checkpoint interval such as `100ms`, `1s`, `2.5s`, or `1m`.

    A bare number is seconds. Anything zero, negative, malformed, or outside the
    supported range is refused here rather than becoming a scheduler that spins
    or never fires.
    """

    if not isinstance(text, str):
        raise CheckpointError("checkpoint interval must be a string")
    candidate = text.strip().lower()
    if not candidate:
        raise CheckpointError("checkpoint interval is empty")
    match = _DURATION.match(candidate)
    if match is None:
        raise CheckpointError(
            f"invalid checkpoint interval {text!r}; expected forms like "
            "'100ms', '1s', '2.5s', '1m'"
        )
    seconds = float(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
    if seconds < MIN_INTERVAL_SECONDS:
        raise CheckpointError(
            f"checkpoint interval {text!r} is below the {MIN_INTERVAL_SECONDS}s minimum"
        )
    if seconds > MAX_INTERVAL_SECONDS:
        raise CheckpointError(
            f"checkpoint interval {text!r} exceeds the {MAX_INTERVAL_SECONDS}s maximum"
        )
    return seconds


def parse_slots(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckpointError("checkpoint slot count must be an integer")
    if not MIN_SLOTS <= value <= MAX_SLOTS:
        raise CheckpointError(
            f"checkpoint slots must be between {MIN_SLOTS} and {MAX_SLOTS}; "
            f"got {value}. Two slots are the minimum that can keep a committed "
            "checkpoint while writing the next one."
        )
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slot_filename(index: int) -> str:
    if index < len(SLOT_NAMES):
        return f"{SLOT_NAMES[index]}{SLOT_SUFFIX}"
    return f"slot-{index + 1:02d}{SLOT_SUFFIX}"


@dataclass(frozen=True)
class CheckpointCommitResult:
    """What one commit attempt actually achieved."""

    generation: int
    previous_generation: int | None
    slot: str
    lineage_id: str
    pause_seconds: float
    commit_seconds: float
    image_bytes: int
    directory_fsync: str
    committed_at: str

    @property
    def durable(self) -> bool:
        """True only when the directory entry was flushed as well as the file.

        When this is False the image contents reached stable storage but the
        rename that publishes them may not have, so a power cut can still lose
        the newest generation. The previous generation remains committed.
        """

        return self.directory_fsync == DIRECTORY_FSYNC_SUPPORTED


@dataclass(frozen=True)
class SlotInspection:
    """The outcome of validating one slot file."""

    slot: str
    path: Path
    present: bool
    valid: bool
    generation: int | None = None
    lineage_id: str | None = None
    created_at: str | None = None
    previous_generation: int | None = None
    directory_fsync: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CheckpointRecoveryResult:
    selected: SlotInspection | None
    candidates: tuple[SlotInspection, ...]
    lineage_id: str | None
    refusals: tuple[str, ...] = ()


@dataclass
class CheckpointStatus:
    """Live status for `continuum checkpoints`, kept by the scheduler."""

    enabled: bool = True
    directory: str = ""
    requested_interval_seconds: float = 0.0
    slots: int = MIN_SLOTS
    failure_policy: str = FAILURE_CONTINUE
    lineage_id: str = ""
    writing: bool = False
    last_generation: int | None = None
    last_committed_at: str | None = None
    last_duration_seconds: float | None = None
    last_pause_seconds: float | None = None
    last_slot: str | None = None
    fallback_slot: str | None = None
    last_error: str | None = None
    commits: int = 0
    failures: int = 0
    coalesced_ticks: int = 0
    directory_fsync: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def probe_directory_fsync(directory: Path) -> str:
    """Decide whether this platform can flush a directory entry.

    Distinguishes "this platform has no such operation" from "the I/O failed",
    which matters because only the first is an acceptable weaker guarantee. A
    real I/O error is raised, never downgraded into the unsupported answer.
    """

    if os.name == "nt":
        # Windows has no directory handle that can be flushed this way. The
        # atomic replace is still ordered, but the weaker guarantee is recorded
        # rather than papered over.
        return DIRECTORY_FSYNC_UNSUPPORTED
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        raise CheckpointError(
            f"cannot open checkpoint directory for durability probe: {exc}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}:
            return DIRECTORY_FSYNC_UNSUPPORTED
        raise CheckpointError(
            f"checkpoint directory flush failed: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    return DIRECTORY_FSYNC_SUPPORTED


def _fsync_directory(directory: Path, capability: str) -> None:
    """Flush the directory entry, honouring a previously probed capability."""

    if capability == DIRECTORY_FSYNC_UNSUPPORTED:
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        # The capability probe already succeeded, so this is a genuine I/O
        # failure and the commit must not be reported as durable.
        raise CheckpointError(f"checkpoint directory flush failed: {exc}") from exc
    finally:
        os.close(descriptor)


class CheckpointStore:
    """A crash-safe rolling checkpoint directory.

    Invariant: at every instant, at least one fully committed and independently
    valid checkpoint exists on disk once one has ever been committed. New images
    are written to a temporary file in the same directory and atomically
    replace the *oldest* slot, so the newest committed checkpoint is never the
    one being overwritten.
    """

    def __init__(self, directory: str | os.PathLike[str], *, slots: int = MIN_SLOTS):
        self.slots = parse_slots(slots)
        self.directory = Path(directory).expanduser().resolve()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CheckpointError(
                f"cannot create checkpoint directory {self.directory}: {exc}"
            ) from exc
        if not self.directory.is_dir():
            raise CheckpointError(
                f"checkpoint path is not a directory: {self.directory}"
            )
        self.slot_paths = [
            self.directory / slot_filename(index) for index in range(self.slots)
        ]
        self.directory_fsync = probe_directory_fsync(self.directory)

    # ------------------------------------------------------------------ scan

    def inspect_slots(self) -> list[SlotInspection]:
        """Validate every slot independently, trusting nothing outside the file.

        Selection never consults filenames, mtimes, directory order, file size,
        or any external metadata document. A slot contributes a generation only
        if the whole container passes the ordinary reader -- bounded ZIP
        handling, per-entry checksums, document cross-checks -- and then carries
        a structurally valid checkpoint block.
        """

        results: list[SlotInspection] = []
        for path in self.slot_paths:
            slot = path.name
            if not path.exists():
                results.append(
                    SlotInspection(slot, path, present=False, valid=False,
                                   reason="absent")
                )
                continue
            if path.is_symlink() or not path.is_file():
                results.append(
                    SlotInspection(
                        slot, path, present=True, valid=False,
                        reason="slot is not a regular file",
                    )
                )
                continue
            try:
                loaded = load_image(path)
            except ImageError as exc:
                results.append(
                    SlotInspection(slot, path, present=True, valid=False,
                                   reason=str(exc))
                )
                continue
            except OSError as exc:
                results.append(
                    SlotInspection(slot, path, present=True, valid=False,
                                   reason=f"unreadable: {exc}")
                )
                continue
            block = loaded.manifest.get("checkpoint")
            if block is None:
                results.append(
                    SlotInspection(
                        slot, path, present=True, valid=False,
                        reason="image carries no checkpoint metadata",
                    )
                )
                continue
            # load_image already validated the block structurally; reaching here
            # means generation and lineage are well-formed and authenticated.
            results.append(
                SlotInspection(
                    slot,
                    path,
                    present=True,
                    valid=True,
                    generation=block["generation"],
                    lineage_id=block["lineage_id"],
                    created_at=block["created_at"],
                    previous_generation=block.get("previous_generation"),
                    directory_fsync=block["durability"]["directory_fsync"],
                )
            )
        return results

    def recover(self, *, lineage_id: str | None = None) -> CheckpointRecoveryResult:
        """Select the highest fully valid generation.

        Falls back to an older slot when a newer one is corrupt, and reports why
        each rejected candidate was refused. Slots from a different lineage are
        refused rather than mixed, so a directory that accidentally accumulated
        two unrelated sessions cannot resume one from the other's state.
        """

        candidates = self.inspect_slots()
        valid = [item for item in candidates if item.valid]
        refusals = [
            f"{item.slot}: {item.reason}"
            for item in candidates
            if not item.valid and item.present
        ]
        if not valid:
            return CheckpointRecoveryResult(
                None, tuple(candidates), lineage_id, tuple(refusals)
            )
        if lineage_id is None:
            # Trust the majority lineage rather than whichever slot happens to
            # hold the highest generation, so a single injected foreign slot
            # cannot capture the directory by claiming a huge generation.
            counts: dict[str, int] = {}
            for item in valid:
                counts[item.lineage_id] = counts.get(item.lineage_id, 0) + 1
            best = max(counts.values())
            tied = sorted(name for name, count in counts.items() if count == best)
            if len(tied) > 1:
                return CheckpointRecoveryResult(
                    None,
                    tuple(candidates),
                    None,
                    tuple(
                        refusals
                        + [
                            "refusing to choose between unrelated lineages "
                            f"{tied}; pass an explicit lineage to disambiguate"
                        ]
                    ),
                )
            lineage_id = tied[0]
        matching = []
        for item in valid:
            if item.lineage_id == lineage_id:
                matching.append(item)
            else:
                refusals.append(
                    f"{item.slot}: generation {item.generation} belongs to "
                    f"lineage {item.lineage_id!r}, not {lineage_id!r}"
                )
        if not matching:
            return CheckpointRecoveryResult(
                None, tuple(candidates), lineage_id, tuple(refusals)
            )
        highest = max(item.generation for item in matching)
        tied_slots = sorted(
            item.slot for item in matching if item.generation == highest
        )
        if len(tied_slots) > 1:
            # Two committed slots claiming one generation is not a state this
            # writer can produce. Refuse deterministically rather than guess.
            refusals.append(
                f"generation {highest} appears in more than one slot "
                f"({', '.join(tied_slots)}); refusing to guess which is current"
            )
            return CheckpointRecoveryResult(
                None, tuple(candidates), lineage_id, tuple(refusals)
            )
        selected = next(item for item in matching if item.generation == highest)
        for item in matching:
            if item is not selected:
                refusals.append(
                    f"{item.slot}: generation {item.generation} is older than "
                    f"the selected generation {highest}"
                )
        return CheckpointRecoveryResult(
            selected, tuple(candidates), lineage_id, tuple(refusals)
        )

    # ---------------------------------------------------------------- commit

    def next_generation(self, *, lineage_id: str | None = None) -> int:
        result = self.recover(lineage_id=lineage_id)
        if result.selected is None:
            return 1
        return result.selected.generation + 1

    def target_slot(self, *, lineage_id: str | None = None) -> Path:
        """Pick the slot to overwrite: the oldest, never the newest valid one."""

        inspections = self.inspect_slots()
        empty = [item for item in inspections if not item.present]
        if empty:
            return empty[0].path
        invalid = [item for item in inspections if not item.valid]
        if invalid:
            return invalid[0].path
        relevant = [
            item
            for item in inspections
            if lineage_id is None or item.lineage_id == lineage_id
        ] or inspections
        return min(relevant, key=lambda item: item.generation).path

    def cleanup_temporaries(self) -> list[str]:
        """Remove abandoned temporary files left by an interrupted commit.

        Only this store's own temporary prefix is touched, and only regular
        files, so a stray directory or an unrelated file is reported rather than
        deleted.
        """

        removed: list[str] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError as exc:
            raise CheckpointError(
                f"cannot scan checkpoint directory {self.directory}: {exc}"
            ) from exc
        for entry in entries:
            if not entry.name.startswith(TEMPORARY_PREFIX):
                continue
            if not entry.name.endswith(TEMPORARY_SUFFIX):
                continue
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise CheckpointError(
                    f"cannot remove stale checkpoint temporary {entry}: {exc}"
                ) from exc
            removed.append(entry.name)
        return removed

    def commit(
        self,
        vm: Any,
        source: str,
        *,
        lineage_id: str,
        generation: int,
        previous_generation: int | None,
        requested_interval_seconds: float,
    ) -> CheckpointCommitResult:
        """Serialize the stopped VM and durably publish it into a slot.

        The ordering is the whole point, so it is spelled out:

        1. choose the oldest slot, never the newest committed one
        2. write the image to a temporary file in the same directory
        3. finish all contents and checksums
        4. flush the temporary file's contents to stable storage
        5. atomically replace the chosen slot
        6. flush the directory entry where the platform supports it
        7. only then report the generation as committed

        `save_image` performs 2-4 for the temporary path; this method performs
        1 and 5-7. A crash at any point leaves either the old slot contents or
        the new ones, never a mix: the replace is atomic and the temporary file
        is never a slot name, so a partial write is not a recovery candidate.
        """

        started = time.monotonic()
        destination = self.target_slot(lineage_id=lineage_id)
        block = {
            "checkpoint_format_version": CHECKPOINT_METADATA_VERSION,
            "mode": CHECKPOINT_MODE_PERIODIC,
            "lineage_id": lineage_id,
            "generation": generation,
            "previous_generation": previous_generation,
            "created_at": _utc_now(),
            "requested_interval_seconds": float(requested_interval_seconds),
            "durability": {
                "file_fsync": True,
                "directory_fsync": self.directory_fsync,
            },
        }

        temporary = self.directory / (
            f"{TEMPORARY_PREFIX}{uuid.uuid4().hex}{TEMPORARY_SUFFIX}"
        )
        committed = False
        try:
            # Inside the try so that an I/O failure here is classified and
            # subject to the configured failure policy, exactly like a failure
            # at any later stage. An unclassified OSError escaping the store
            # would kill a healthy program under the `continue` policy.
            _stage(STAGE_BEFORE_TEMPORARY)
            _stage(STAGE_DURING_SERIALIZATION)
            # save_image writes, checksums, flushes, and atomically installs the
            # temporary path. Reusing it keeps one writer and one integrity
            # implementation for both freeze and checkpoint.
            try:
                save_image(temporary, vm, source, checkpoint=block)
            except ImageError as exc:
                # A state that cannot be serialized is a checkpoint failure, not
                # a program failure, so it goes through the configured policy
                # rather than escaping and killing a healthy program.
                raise CheckpointError(
                    f"checkpoint serialization failed: {exc}"
                ) from exc
            _stage(STAGE_AFTER_TEMPORARY_WRITE)
            image_bytes = temporary.stat().st_size
            with open(temporary, "r+b") as handle:
                os.fsync(handle.fileno())
            _stage(STAGE_AFTER_FLUSH)
            pause_seconds = time.monotonic() - started
            _stage(STAGE_DURING_RENAME)
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise CheckpointError(
                    self._describe_write_failure(exc, destination)
                ) from exc
            committed = True
            _stage(STAGE_AFTER_RENAME)
            _fsync_directory(self.directory, self.directory_fsync)
            _stage(STAGE_AFTER_DIRECTORY_FLUSH)
        except OSError as exc:
            raise CheckpointError(
                self._describe_write_failure(exc, destination)
            ) from exc
        finally:
            if not committed:
                try:
                    temporary.unlink()
                except (FileNotFoundError, OSError):
                    # A temporary that cannot be removed now is removed by
                    # cleanup_temporaries() on the next start. It is never a
                    # recovery candidate, so leaving it is safe.
                    pass
        return CheckpointCommitResult(
            generation=generation,
            previous_generation=previous_generation,
            slot=destination.name,
            lineage_id=lineage_id,
            pause_seconds=pause_seconds,
            commit_seconds=time.monotonic() - started,
            image_bytes=image_bytes,
            directory_fsync=self.directory_fsync,
            committed_at=block["created_at"],
        )

    def _describe_write_failure(self, exc: OSError, destination: Path) -> str:
        """Turn a raw errno into something an operator can act on."""

        code = getattr(exc, "errno", None)
        if code == errno.ENOSPC:
            return (
                f"checkpoint directory {self.directory} is out of space; "
                "the previously committed checkpoint is unchanged"
            )
        if code == errno.EDQUOT:
            return (
                f"disk quota exceeded writing {destination.name}; "
                "the previously committed checkpoint is unchanged"
            )
        if code in {errno.EACCES, errno.EPERM}:
            return f"permission denied writing {destination}: {exc}"
        if code == errno.EROFS:
            return f"checkpoint directory {self.directory} is read-only"
        if code == errno.EXDEV:
            return (
                f"cannot atomically replace {destination.name} across "
                "filesystems; the checkpoint directory and its temporary files "
                "must live on one filesystem"
            )
        return f"checkpoint write failed for {destination.name}: {exc}"


class CheckpointScheduler:
    """Decides when a checkpoint is due and performs it at a safe point.

    Called synchronously from the VM safe-point callback. Because the call is
    synchronous there can never be two checkpoints in flight, so "do not start a
    new checkpoint while the previous one is still writing" is a property of the
    design rather than a lock that could be got wrong.

    Missed ticks coalesce: the next deadline is computed from the previous
    deadline to avoid drift, but is pulled forward to now whenever the previous
    commit overran, so a slow checkpoint produces one late checkpoint rather
    than a backlog of them.
    """

    def __init__(
        self,
        store: CheckpointStore,
        source: str,
        *,
        lineage_id: str,
        interval_seconds: float,
        failure_policy: str = FAILURE_CONTINUE,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        if failure_policy not in FAILURE_POLICIES:
            raise CheckpointError(
                f"unknown checkpoint failure policy {failure_policy!r}; "
                f"expected one of {list(FAILURE_POLICIES)}"
            )
        self.store = store
        self.source = source
        self.lineage_id = lineage_id
        self.interval = interval_seconds
        self.failure_policy = failure_policy
        self._clock = clock
        self._on_event = on_event
        self._generation = store.next_generation(lineage_id=lineage_id) - 1
        self._deadline = clock() + interval_seconds
        self._stopped = False
        self.status = CheckpointStatus(
            enabled=True,
            directory=str(store.directory),
            requested_interval_seconds=interval_seconds,
            slots=store.slots,
            failure_policy=failure_policy,
            lineage_id=lineage_id,
            last_generation=self._generation or None,
            directory_fsync=store.directory_fsync,
        )
        self.history: list[CheckpointCommitResult] = []

    @property
    def generation(self) -> int:
        return self._generation

    def due(self) -> bool:
        return not self._stopped and self._clock() >= self._deadline

    def on_safe_point(self, vm: Any) -> None:
        """Checkpoint if one is due. Cheap when it is not."""

        if self._stopped or self._clock() < self._deadline:
            return
        self.checkpoint(vm)

    def checkpoint(self, vm: Any) -> CheckpointCommitResult | None:
        """Perform one checkpoint now, whether or not it was due."""

        if self._stopped:
            return None
        self.status.writing = True
        try:
            result = self.store.commit(
                vm,
                self.source,
                lineage_id=self.lineage_id,
                generation=self._generation + 1,
                previous_generation=self._generation or None,
                requested_interval_seconds=self.interval,
            )
        except CheckpointError as exc:
            self.status.writing = False
            self.status.failures += 1
            self.status.last_error = str(exc)
            self._reschedule()
            self._emit("checkpoint-failed", {"error": str(exc)})
            if self.failure_policy == FAILURE_TERMINATE:
                raise
            return None
        self._generation = result.generation
        self.status.writing = False
        self.status.commits += 1
        self.status.last_generation = result.generation
        self.status.last_committed_at = result.committed_at
        self.status.last_duration_seconds = result.commit_seconds
        self.status.last_pause_seconds = result.pause_seconds
        self.status.last_slot = result.slot
        self.status.last_error = None
        fallback = [
            item
            for item in self.store.inspect_slots()
            if item.valid and item.slot != result.slot
        ]
        self.status.fallback_slot = (
            max(fallback, key=lambda item: item.generation).slot if fallback else None
        )
        self.history.append(result)
        self._reschedule()
        self._emit(
            "checkpoint-committed",
            {
                "generation": result.generation,
                "slot": result.slot,
                "pause_seconds": result.pause_seconds,
                "commit_seconds": result.commit_seconds,
                "image_bytes": result.image_bytes,
                "durable": result.durable,
            },
        )
        return result

    def _reschedule(self) -> None:
        now = self._clock()
        self._deadline += self.interval
        if self._deadline <= now:
            # The commit overran the interval. Collapse every deadline that
            # passed into a single next one instead of firing back to back.
            missed = int((now - self._deadline) // self.interval) + 1
            self.status.coalesced_ticks += missed
            self._deadline = now + self.interval

    def stop(self) -> None:
        """Disable further checkpoints without disturbing the running session.

        Idempotent, and safe to call from a `finally`. Because commits are
        synchronous, this can never interrupt one in progress: control is only
        ever here when no checkpoint is being written.
        """

        self._stopped = True
        self.status.enabled = False
        self.status.writing = False

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)
