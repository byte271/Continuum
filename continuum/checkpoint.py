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
import hashlib
import json
import os
import re
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import (
    CheckpointError,
    ImageError,
    ResourceError,
    UnsupportedObjectError,
)
from .image import (
    CHECKPOINT_METADATA_VERSION,
    CHECKPOINT_MODE_PERIODIC,
    DIRECTORY_FSYNC_SUPPORTED,
    DIRECTORY_FSYNC_UNSUPPORTED,
    MAX_ENTRIES,
    _validate_checkpoint_metadata,
    load_image,
    save_image,
)

# `manifest.json` is a small JSON document; anything near this is malformed or
# hostile, and the cheap reader refuses it rather than decompressing it.
MAX_MANIFEST_SIZE = 1024 * 1024

# Everything `save_image` raises for a state it cannot capture, as opposed to a
# bug in Continuum. These become CheckpointError so the configured failure
# policy decides what happens, instead of the exception escaping through
# `vm.run()` and killing a program that is otherwise healthy.
#
# Deliberately not `Exception`: AssertionError, TypeError, AttributeError and
# friends indicate a defect here and must keep propagating, and
# KeyboardInterrupt/SystemExit are BaseException and are never caught.
#   UnsupportedObjectError - the graph codec refuses a live value
#   ResourceError          - a tracked resource cannot be snapshotted
#   ImageError             - the image cannot be constructed or re-decoded
#   RecursionError         - the live graph is too deep to encode; a property
#                            of the data, not of the code
CAPTURE_FAILURES = (
    UnsupportedObjectError,
    ResourceError,
    ImageError,
    RecursionError,
)

SLOT_NAMES = ("slot-a", "slot-b")
SLOT_SUFFIX = ".cont"
TEMPORARY_PREFIX = ".checkpoint-"
TEMPORARY_SUFFIX = ".tmp"
MIN_INTERVAL_SECONDS = 0.001
MAX_INTERVAL_SECONDS = 86_400.0
# Exactly two committed slots. Two is the minimum that can hold a committed
# checkpoint while the next one is written, and more than two bought nothing:
# the rotation only ever needs "the one being written" and "the one to fall
# back to". A configurable count also has to be discovered by `recover` and
# `checkpoints`, and the only trustworthy place to record it is inside the
# images -- which cannot be read until the slots are already known. Rather than
# invent an unauthenticated slot-count file or scan the directory for anything
# that looks like a slot, the count is fixed.
SLOT_COUNT = 2
MIN_SLOTS = SLOT_COUNT
MAX_SLOTS = SLOT_COUNT
# Bounded so a process that checkpoints every 100ms for weeks cannot grow this
# without limit. `status` keeps the lifetime totals; this is a recent window for
# reporting and for the benchmark, which reads only what it just produced.
HISTORY_LIMIT = 256

FAILURE_CONTINUE = "continue"
FAILURE_TERMINATE = "terminate"
FAILURE_POLICIES = (FAILURE_CONTINUE, FAILURE_TERMINATE)

# Why recovery did not produce a checkpoint. Only OUTCOME_EMPTY means "there is
# genuinely nothing here"; every other value means state exists and was
# refused, which an explicit recovery request must treat as an error rather
# than as licence to start the program over from the beginning.
OUTCOME_RECOVERED = "recovered"
OUTCOME_EMPTY = "empty-directory"
OUTCOME_NO_VALID_CHECKPOINT = "no-valid-checkpoint"
OUTCOME_AMBIGUOUS_LINEAGE = "ambiguous-lineage"
OUTCOME_DUPLICATE_GENERATION = "duplicate-generation"
OUTCOME_CORRUPT = "corrupt"
OUTCOME_LINEAGE_NOT_PRESENT = "lineage-not-present"
RECOVERY_OUTCOMES = (
    OUTCOME_RECOVERED,
    OUTCOME_EMPTY,
    OUTCOME_NO_VALID_CHECKPOINT,
    OUTCOME_AMBIGUOUS_LINEAGE,
    OUTCOME_DUPLICATE_GENERATION,
    OUTCOME_CORRUPT,
    OUTCOME_LINEAGE_NOT_PRESENT,
)

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
    """Accept only the fixed slot count.

    See SLOT_COUNT: a variable count cannot be discovered by `recover` without
    either an unauthenticated metadata file or guessing at filenames, and both
    were rejected. Refusing other values here keeps `run`, `recover`, and
    `checkpoints` incapable of disagreeing about how many slots exist.
    """

    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckpointError("checkpoint slot count must be an integer")
    if value != SLOT_COUNT:
        raise CheckpointError(
            f"checkpoint slots must be exactly {SLOT_COUNT}; got {value}. Two "
            "slots are what the rotation needs: one being written and one to "
            "fall back to. A different count cannot be discovered safely at "
            "recovery time, so it is refused rather than silently ignored."
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
    """What one commit attempt actually achieved.

    `pause_seconds` is the **complete** stop-the-world duration: the VM is
    stopped for the whole synchronous callback, so it covers serialization, the
    file flush, the atomic rename, and the directory flush. `commit_seconds` is
    the same span, kept as a separate name because it is what the durability
    protocol cost. The three phase fields below break that total down and are
    named for exactly what they measure, so no partial duration is ever
    reported as the pause.
    """

    generation: int
    previous_generation: int | None
    slot: str
    lineage_id: str
    pause_seconds: float
    commit_seconds: float
    serialization_seconds: float
    file_flush_seconds: float
    durable_publish_seconds: float
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
    """Why recovery did or did not select a checkpoint.

    `outcome` exists so a caller can tell "this directory is empty, starting
    fresh is correct" apart from "there is state here that I refused to use".
    An explicit recovery request must never treat the second as the first and
    silently restart the program from its entry point.
    """

    selected: SlotInspection | None
    candidates: tuple[SlotInspection, ...]
    lineage_id: str | None
    refusals: tuple[str, ...] = ()
    outcome: str = OUTCOME_RECOVERED

    @property
    def recoverable(self) -> bool:
        return self.selected is not None

    @property
    def is_clean_start(self) -> bool:
        """True only for a directory with nothing in it at all."""

        return self.outcome == OUTCOME_EMPTY


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
    # Set when a generation reached a slot but its durability confirmation
    # failed. The image is readable; the rename may not have been flushed.
    last_published_without_durability: int | None = None
    history_limit: int = HISTORY_LIMIT

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


def _errno_is(code: int | None, name: str) -> bool:
    """Compare against an errno constant that may not exist on this platform.

    `errno` only defines what the underlying C library defines, so `EDQUOT` in
    particular is absent on some builds. A bare `errno.EDQUOT` would raise
    AttributeError from inside the error-formatting path and replace the real
    I/O failure with an unrelated traceback -- the operator would lose the
    actual reason the checkpoint could not be written.
    """

    if code is None:
        return False
    expected = getattr(errno, name, None)
    return expected is not None and code == expected


def describe_write_failure(
    exc: OSError, destination: Path, directory: Path
) -> str:
    """Turn a raw errno into something an operator can act on.

    Module level and pure so it can be tested directly, including against
    errno names this platform does not define.
    """

    code = getattr(exc, "errno", None)
    if _errno_is(code, "ENOSPC"):
        return (
            f"checkpoint directory {directory} is out of space; the "
            "previously committed checkpoint is unchanged"
        )
    if _errno_is(code, "EDQUOT"):
        return (
            f"disk quota exceeded writing {destination.name}; the previously "
            "committed checkpoint is unchanged"
        )
    if _errno_is(code, "EACCES") or _errno_is(code, "EPERM"):
        return f"permission denied writing {destination}: {exc}"
    if _errno_is(code, "EROFS"):
        return f"checkpoint directory {directory} is read-only"
    if _errno_is(code, "EXDEV"):
        return (
            f"cannot atomically replace {destination.name} across filesystems; "
            "the checkpoint directory and its temporary files must live on one "
            "filesystem"
        )
    return f"checkpoint write failed for {destination.name}: {exc}"


def _read_verified_manifest_block(path: Path) -> dict[str, Any] | None:
    """Read `manifest.json`, verify its digest, return its checkpoint block.

    Bounded on purpose: it refuses an archive with an implausible entry count,
    reads only two named entries, caps how much it will decompress, and checks
    the manifest against `checksums.json` before parsing anything out of it. It
    is not a substitute for `load_image` and is never used to select an image
    for restore -- see `CheckpointStore.slot_hints`.
    """

    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise ImageError("image has an invalid number of entries")
        by_name = {info.filename: info for info in infos}
        for required in ("manifest.json", "checksums.json"):
            info = by_name.get(required)
            if info is None:
                raise ImageError(f"image is missing {required}")
            if info.file_size > MAX_MANIFEST_SIZE:
                raise ImageError(f"{required} is implausibly large")
        raw_manifest = archive.read("manifest.json")
        raw_checksums = archive.read("checksums.json")
    checksums = json.loads(raw_checksums)
    if (
        not isinstance(checksums, dict)
        or checksums.get("algorithm") != "sha256"
        or not isinstance(checksums.get("entries"), dict)
    ):
        raise ImageError("invalid checksum document")
    expected = checksums["entries"].get("manifest.json")
    if not isinstance(expected, str) or hashlib.sha256(raw_manifest).hexdigest() != expected:
        raise ImageError("manifest digest does not match the checksum document")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict):
        raise ImageError("manifest is not an object")
    block = manifest.get("checkpoint")
    if block is None:
        return None
    # Same structural validation the full reader applies, so a hint can never
    # be looser about what counts as a well-formed block than recovery is.
    return _validate_checkpoint_metadata(block)


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

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        slots: int = SLOT_COUNT,
        clock: Callable[[], float] = time.monotonic,
    ):
        # Injectable so the phase timings can be asserted exactly. Windows
        # `time.monotonic()` has ~15.6ms granularity, which is coarser than a
        # whole commit: real readings collapse to identical values there, so a
        # test comparing two phases by wall time is measuring the timer, not
        # the code.
        self._clock = clock
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

    def slot_hints(self) -> list[SlotInspection]:
        """Cheap slot summary for the commit path. **Never recovery evidence.**

        Reads two small entries -- `manifest.json` and `checksums.json` -- and
        verifies the manifest against its recorded digest, instead of
        decompressing and checksumming every entry the way `load_image` does.
        That is enough to choose which slot to overwrite and to name the
        fallback slot in status, and it keeps that work off the stop-the-world
        pause: `inspect_slots` grows with image size and entry count, and the
        commit path used to call it twice per checkpoint.

        The manifest digest *is* verified, so a corrupted or forged manifest is
        not silently trusted here either. But this deliberately does not check
        the heap, frames, IR, resources, or their cross-document agreement, so
        a slot it calls `valid` may still be unrestorable. It is an operational
        hint only. `recover` uses `inspect_slots`, which validates the whole
        container, and nothing selects a checkpoint for restore from here.
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
                    SlotInspection(slot, path, present=True, valid=False,
                                   reason="slot is not a regular file")
                )
                continue
            try:
                block = _read_verified_manifest_block(path)
            except (ImageError, OSError, zipfile.BadZipFile, KeyError) as exc:
                results.append(
                    SlotInspection(slot, path, present=True, valid=False,
                                   reason=f"unreadable manifest: {exc}")
                )
                continue
            if block is None:
                results.append(
                    SlotInspection(slot, path, present=True, valid=False,
                                   reason="image carries no checkpoint metadata")
                )
                continue
            results.append(
                SlotInspection(
                    slot, path, present=True, valid=True,
                    generation=block["generation"],
                    lineage_id=block["lineage_id"],
                    created_at=block["created_at"],
                    previous_generation=block.get("previous_generation"),
                    directory_fsync=block["durability"]["directory_fsync"],
                )
            )
        return results

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
            # An empty directory is a legitimate fresh start. A directory that
            # holds files which failed validation is not, and must not be
            # reported as one.
            present = [item for item in candidates if item.present]
            outcome = OUTCOME_EMPTY if not present else OUTCOME_CORRUPT
            return CheckpointRecoveryResult(
                None, tuple(candidates), lineage_id, tuple(refusals), outcome
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
                    (
                        *refusals,
                        "refusing to choose between unrelated lineages "
                        f"{tied}; pass an explicit lineage to disambiguate",
                    ),
                    OUTCOME_AMBIGUOUS_LINEAGE,
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
                None, tuple(candidates), lineage_id, tuple(refusals),
                OUTCOME_LINEAGE_NOT_PRESENT,
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
                None, tuple(candidates), lineage_id, tuple(refusals),
                OUTCOME_DUPLICATE_GENERATION,
            )
        selected = next(item for item in matching if item.generation == highest)
        for item in matching:
            if item is not selected:
                refusals.append(
                    f"{item.slot}: generation {item.generation} is older than "
                    f"the selected generation {highest}"
                )
        return CheckpointRecoveryResult(
            selected, tuple(candidates), lineage_id, tuple(refusals),
            OUTCOME_RECOVERED,
        )

    # ---------------------------------------------------------------- commit

    def next_generation(self, *, lineage_id: str | None = None) -> int:
        result = self.recover(lineage_id=lineage_id)
        if result.selected is None:
            return 1
        return result.selected.generation + 1

    def target_slot(self, *, lineage_id: str | None = None) -> Path:
        """Pick the slot to overwrite: the oldest, never the newest valid one.

        Uses `slot_hints`, not `inspect_slots`: this runs inside the
        stop-the-world pause and only needs generation and lineage. A slot the
        hint wrongly calls valid can at worst be preserved rather than
        overwritten, which is the safe direction -- it never causes the newest
        committed checkpoint to be chosen for overwrite, because a hint that
        cannot read a slot's generation reports it invalid and invalid slots
        are overwritten first.
        """

        inspections = self.slot_hints()
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

    def claim_for_new_lineage(self, lineage_id: str) -> None:
        """Refuse to start a fresh lineage in a directory another one owns.

        Without this, a `run` that reuses a populated directory without
        `--recover-latest` starts a new lineage and then only ever overwrites
        the one slot that already matches it, because `target_slot` prefers the
        lineage being written. The foreign slots stay forever: with two slots
        that leaves recovery permanently tied and refusing the directory, and
        the operator's old checkpoints are silently useless.

        Nothing is deleted here. The user is told to recover the existing
        lineage, point at an empty directory, or remove it themselves --
        destroying someone's only crash-recovery state is not a decision this
        code gets to make quietly.
        """

        foreign = sorted(
            {
                item.lineage_id
                for item in self.slot_hints()
                if item.valid and item.lineage_id != lineage_id
            }
        )
        if not foreign:
            return
        raise CheckpointError(
            f"checkpoint directory {self.directory} already holds committed "
            f"checkpoints for lineage {', '.join(repr(name) for name in foreign)}. "
            "Starting a new lineage here would leave those slots stranded and "
            "make recovery ambiguous. Resume them with --recover-latest (or "
            "`continuum recover`), choose an empty directory, or remove the "
            "existing checkpoints yourself."
        )

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

        started = self._clock()
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
            except CAPTURE_FAILURES as exc:
                # A state that cannot be captured is a checkpoint failure, not a
                # program failure, so it goes through the configured policy
                # rather than escaping and killing a healthy program. The
                # original is preserved as __cause__ and named in the message.
                raise CheckpointError(
                    f"checkpoint capture failed "
                    f"({type(exc).__name__}): {exc}"
                ) from exc
            _stage(STAGE_AFTER_TEMPORARY_WRITE)
            serialization_seconds = self._clock() - started
            image_bytes = temporary.stat().st_size
            with open(temporary, "r+b") as handle:
                os.fsync(handle.fileno())
            _stage(STAGE_AFTER_FLUSH)
            flush_seconds = self._clock() - started
            _stage(STAGE_DURING_RENAME)
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise CheckpointError(
                    self._describe_write_failure(exc, destination)
                ) from exc
            # From here the image is installed under a slot name and any reader
            # can see this generation. Every failure below must therefore carry
            # the published generation back to the caller.
            committed = True
            _stage(STAGE_AFTER_RENAME)
            try:
                _fsync_directory(self.directory, self.directory_fsync)
                _stage(STAGE_AFTER_DIRECTORY_FLUSH)
            except CheckpointError as exc:
                exc.published_generation = generation
                exc.published_slot = destination.name
                raise
        except CheckpointError as exc:
            if committed and exc.published_generation is None:
                exc.published_generation = generation
                exc.published_slot = destination.name
            raise
        except OSError as exc:
            raise CheckpointError(
                self._describe_write_failure(exc, destination),
                published_generation=generation if committed else None,
                published_slot=destination.name if committed else None,
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
        # The VM is stopped for this entire call, so the pause is the whole
        # thing -- serialization, flush, rename, and directory flush included.
        # Measuring only up to the flush understated it, and the directory
        # flush is the slowest durability step on many filesystems.
        elapsed = self._clock() - started
        return CheckpointCommitResult(
            generation=generation,
            previous_generation=previous_generation,
            slot=destination.name,
            lineage_id=lineage_id,
            pause_seconds=elapsed,
            commit_seconds=elapsed,
            serialization_seconds=serialization_seconds,
            file_flush_seconds=flush_seconds - serialization_seconds,
            durable_publish_seconds=elapsed - flush_seconds,
            image_bytes=image_bytes,
            directory_fsync=self.directory_fsync,
            committed_at=block["created_at"],
        )

    def _describe_write_failure(self, exc: OSError, destination: Path) -> str:
        """Turn a raw errno into something an operator can act on."""

        return describe_write_failure(exc, destination, self.directory)


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
        # Bounded: lifetime totals live in `status`, so only a recent
        # window is retained. An unbounded list would gain 36,000 records
        # an hour at a 100ms interval in a process designed to run for
        # weeks.
        self.history: deque[CheckpointCommitResult] = deque(maxlen=HISTORY_LIMIT)

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
            # If the rename already installed this generation, it is visible to
            # every reader whatever happened afterwards. Advance past it, or the
            # retry would publish the same number into the other slot and leave
            # two valid slots claiming one generation -- a state recovery
            # refuses outright, which would turn one transient flush error into
            # a permanently unrecoverable directory.
            if exc.published_generation is not None:
                self._generation = exc.published_generation
                self.status.last_generation = exc.published_generation
                self.status.last_slot = exc.published_slot
                self.status.last_published_without_durability = (
                    exc.published_generation
                )
            self.status.writing = False
            self.status.failures += 1
            self.status.last_error = str(exc)
            self._reschedule()
            self._emit(
                "checkpoint-failed",
                {
                    "error": str(exc),
                    "published_generation": exc.published_generation,
                },
            )
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
        # Cheap hints: this is still inside the stop-the-world pause, and
        # naming the fallback slot does not need every entry checksummed.
        fallback = [
            item
            for item in self.store.slot_hints()
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
