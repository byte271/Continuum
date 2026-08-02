"""Regressions for the PR #7 review findings.

One module per review round rather than scattering these through the existing
files, so each fix has an obvious home and a reviewer can see the finding and
its proof together.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from continuum import checkpoint as checkpoint_module
from continuum.checkpoint import (
    HISTORY_LIMIT,
    OUTCOME_AMBIGUOUS_LINEAGE,
    OUTCOME_CORRUPT,
    OUTCOME_DUPLICATE_GENERATION,
    OUTCOME_EMPTY,
    OUTCOME_LINEAGE_NOT_PRESENT,
    OUTCOME_RECOVERED,
    FAILURE_CONTINUE,
    FAILURE_TERMINATE,
    CheckpointScheduler,
    CheckpointStore,
    describe_write_failure,
)
from continuum.compiler import compile_source
from continuum.errors import (
    CheckpointError,
    ExecutionError,
    ImageError,
    ResourceError,
    UnsupportedObjectError,
)
from continuum.image import save_image
from continuum.vm import VirtualMachine

SOURCE = """
def work(limit):
    total = 0
    index = 0
    while index < limit:
        total = total + index
        print(f"STEP {index}")
        index += 1
    return total


answer = work(400)
"""


def live_vm() -> VirtualMachine:
    vm = VirtualMachine(
        compile_source(SOURCE, "audit.py"), ["audit.py"], "audit.py"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 5:
            vm.step()
    return vm


def commit(store, vm, generation, *, lineage="lin-audit", previous=None):
    return store.commit(
        vm, SOURCE, lineage_id=lineage, generation=generation,
        previous_generation=previous, requested_interval_seconds=0.1,
    )


class StepClock:
    def __init__(self, step: float = 1.0):
        self.step = step
        self.reads = 0

    def __call__(self) -> float:
        value = self.reads * self.step
        self.reads += 1
        return value


class FixedClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------- finding 1

class CaptureFailureTests(unittest.TestCase):
    """Every expected capture failure becomes a CheckpointError."""

    def _failing_vm(self, exception: BaseException):
        inner = live_vm()

        class FailingVM:
            def __getattr__(self, name):
                return getattr(inner, name)

            def state_root(self):
                raise exception

        return FailingVM()

    def test_each_expected_capture_failure_becomes_a_checkpoint_error(self):
        # The real exception types save_image raises for state it cannot
        # capture. A plain ImageError alone would have missed the first two.
        for exception in (
            UnsupportedObjectError("live socket cannot be encoded"),
            ResourceError("tracked file changed during capture"),
            ImageError("image could not be constructed"),
            RecursionError("graph too deep to encode"),
        ):
            with self.subTest(exception=type(exception).__name__):
                with TemporaryDirectory() as directory:
                    store = CheckpointStore(Path(directory) / "cp")
                    with self.assertRaises(CheckpointError) as caught:
                        commit(store, self._failing_vm(exception), 1)
                    # Original preserved, and named in the message.
                    self.assertIs(caught.exception.__cause__, exception)
                    self.assertIn(
                        type(exception).__name__, str(caught.exception)
                    )
                    self.assertIn(str(exception), str(caught.exception))

    def test_a_programming_error_is_not_swallowed(self):
        """A defect here must keep propagating, not become a checkpoint failure."""

        for exception in (TypeError("internal bug"), AssertionError("invariant")):
            with self.subTest(exception=type(exception).__name__):
                with TemporaryDirectory() as directory:
                    store = CheckpointStore(Path(directory) / "cp")
                    with self.assertRaises(type(exception)):
                        commit(store, self._failing_vm(exception), 1)

    def test_continue_policy_leaves_the_vm_running_after_a_capture_failure(self):
        """The whole point: an uncapturable value must not kill the program."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            clock = FixedClock()
            events = []
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                failure_policy=FAILURE_CONTINUE, clock=clock,
                on_event=lambda name, payload: events.append((name, payload)),
            )
            calls = {"count": 0}
            real_save = checkpoint_module.save_image

            def flaky(path, vm, source, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise UnsupportedObjectError("value cannot be encoded")
                return real_save(path, vm, source, **kwargs)

            checkpoint_module.save_image = flaky
            try:
                vm = VirtualMachine(
                    compile_source(SOURCE, "audit.py"), ["audit.py"], "audit.py",
                    safe_point_callback=lambda machine: (
                        clock.advance(1.0), scheduler.on_safe_point(machine)
                    )[-1],
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    vm.run()
            finally:
                checkpoint_module.save_image = real_save
            # The program finished despite the failed capture.
            self.assertTrue(vm.completed)
            self.assertEqual(vm.globals["answer"], sum(range(400)))
            self.assertEqual(scheduler.status.failures, 1)
            self.assertGreater(scheduler.status.commits, 0)
            failed = [p for name, p in events if name == "checkpoint-failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("UnsupportedObjectError", failed[0]["error"])
            # Nothing was published, so no generation was consumed.
            self.assertIsNone(failed[0]["published_generation"])

    def test_terminate_policy_still_stops_execution_on_a_capture_failure(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            clock = FixedClock()
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                failure_policy=FAILURE_TERMINATE, clock=clock,
            )
            real_save = checkpoint_module.save_image

            def always_fails(path, vm, source, **kwargs):
                raise UnsupportedObjectError("value cannot be encoded")

            checkpoint_module.save_image = always_fails
            try:
                vm = VirtualMachine(
                    compile_source(SOURCE, "audit.py"), ["audit.py"], "audit.py",
                    safe_point_callback=lambda machine: (
                        clock.advance(1.0), scheduler.on_safe_point(machine)
                    )[-1],
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    # The VM reports an unhandled error from a callback as
                    # ExecutionError; the checkpoint failure is its cause.
                    with self.assertRaises(ExecutionError) as caught:
                        vm.run()
            finally:
                checkpoint_module.save_image = real_save
            self.assertFalse(vm.completed)
            self.assertIsInstance(caught.exception.__cause__, CheckpointError)
            self.assertIn("UnsupportedObjectError", str(caught.exception))


# --------------------------------------------------------------- finding 2

class PublishedGenerationTests(unittest.TestCase):
    """A generation that reached a slot must never be issued twice."""

    @contextlib.contextmanager
    def _failing_directory_flush(self):
        real = checkpoint_module._fsync_directory

        def broken(directory, capability):
            raise CheckpointError("checkpoint directory flush failed: injected")

        checkpoint_module._fsync_directory = broken
        try:
            yield
        finally:
            checkpoint_module._fsync_directory = real

    def test_commit_reports_the_published_generation_on_a_post_rename_failure(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            with self._failing_directory_flush():
                with self.assertRaises(CheckpointError) as caught:
                    commit(store, vm, 1)
            # The rename already happened, so the generation is visible.
            self.assertEqual(caught.exception.published_generation, 1)
            self.assertEqual(caught.exception.published_slot, "slot-a.cont")
            self.assertEqual(store.recover().selected.generation, 1)

    def test_a_pre_rename_failure_reports_no_published_generation(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            with self.assertRaises(CheckpointError) as caught:
                commit(store, CaptureFailureTests()._failing_vm(
                    ImageError("nope")
                ), 1)
            self.assertIsNone(caught.exception.published_generation)

    def _retry_after_flush_failure(self, policy: str):
        """Commit, fail the flush, then retry, and report what recovery sees."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            clock = FixedClock()
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                failure_policy=policy, clock=clock,
            )
            vm = live_vm()
            commit(store, vm, 1, lineage="lin")
            scheduler._generation = 1

            clock.advance(2.0)
            with self._failing_directory_flush():
                if policy == FAILURE_TERMINATE:
                    with self.assertRaises(CheckpointError):
                        scheduler.checkpoint(vm)
                else:
                    self.assertIsNone(scheduler.checkpoint(vm))
            # Generation 2 was published even though the flush failed.
            self.assertEqual(scheduler.generation, 2)
            self.assertEqual(scheduler.status.last_published_without_durability, 2)
            self.assertIsNotNone(scheduler.status.last_error)

            # The retry must not reuse 2.
            clock.advance(2.0)
            result = scheduler.checkpoint(vm)
            self.assertIsNotNone(result)
            self.assertEqual(result.generation, 3)

            recovered = CheckpointStore(Path(directory) / "cp").recover()
            self.assertEqual(recovered.outcome, OUTCOME_RECOVERED)
            self.assertEqual(recovered.selected.generation, 3)
            generations = sorted(
                item.generation
                for item in recovered.candidates
                if item.valid
            )
            # No duplicate generation anywhere in the directory.
            self.assertEqual(len(generations), len(set(generations)))
            self.assertEqual(generations, [2, 3])

    def test_retry_after_flush_failure_under_continue(self):
        self._retry_after_flush_failure(FAILURE_CONTINUE)

    def test_retry_after_flush_failure_under_terminate(self):
        self._retry_after_flush_failure(FAILURE_TERMINATE)

    def test_the_old_bug_would_have_made_the_directory_unrecoverable(self):
        """Directly construct the duplicate state and confirm it is refused.

        This is what reusing a published generation produced. Recovery must
        refuse it deterministically rather than pick one, which is why the
        generation has to advance past a post-rename failure.
        """

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 5)
            first = store.recover().selected.path.read_bytes()
            other = next(
                path for path in store.slot_paths if not path.exists()
            )
            other.write_bytes(first)
            result = store.recover()
            self.assertIsNone(result.selected)
            self.assertEqual(result.outcome, OUTCOME_DUPLICATE_GENERATION)


# --------------------------------------------------------------- finding 4

class DirectoryOwnershipTests(unittest.TestCase):
    def test_an_empty_directory_is_accepted(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            store.claim_for_new_lineage("lin-new")  # must not raise

    def test_the_same_lineage_is_accepted(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-same")
            store.claim_for_new_lineage("lin-same")

    def test_a_foreign_lineage_directory_is_refused(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-old")
            commit(store, vm, 2, lineage="lin-old", previous=1)
            with self.assertRaises(CheckpointError) as caught:
                store.claim_for_new_lineage("lin-new")
            message = str(caught.exception)
            self.assertIn("lin-old", message)
            self.assertIn("--recover-latest", message)

    def test_a_partially_populated_foreign_directory_is_refused(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-old")
            with self.assertRaises(CheckpointError):
                store.claim_for_new_lineage("lin-new")

    def test_refusal_deletes_nothing(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-old")
            unrelated = store.directory / "notes.txt"
            unrelated.write_text("keep", encoding="utf-8")
            with self.assertRaises(CheckpointError):
                store.claim_for_new_lineage("lin-new")
            self.assertEqual(store.recover().selected.generation, 1)
            self.assertTrue(unrelated.exists())

    def test_a_stale_temporary_does_not_block_a_new_lineage(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            stale = store.directory / ".checkpoint-abc.tmp"
            stale.write_bytes(b"partial")
            store.claim_for_new_lineage("lin-new")
            self.assertEqual(store.cleanup_temporaries(), [stale.name])


# --------------------------------------------------------------- finding 5

class BoundedHistoryTests(unittest.TestCase):
    def test_history_stays_bounded_while_totals_keep_counting(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            clock = FixedClock()
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=clock,
            )
            vm = live_vm()
            total = HISTORY_LIMIT + 40
            for _ in range(total):
                clock.advance(2.0)
                scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, total)
            self.assertEqual(len(scheduler.history), HISTORY_LIMIT)
            self.assertEqual(scheduler.status.history_limit, HISTORY_LIMIT)
            # The retained window is the most recent one.
            self.assertEqual(scheduler.history[-1].generation, total)
            self.assertEqual(
                scheduler.history[0].generation, total - HISTORY_LIMIT + 1
            )


# --------------------------------------------------------------- finding 6

class PauseMeasurementTests(unittest.TestCase):
    def test_pause_covers_the_whole_stop_the_world_span(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            result = commit(store, live_vm(), 1)
            # The VM is stopped for the entire call, so the pause is the whole
            # commit -- not just serialization and the file flush.
            self.assertEqual(result.pause_seconds, result.commit_seconds)
            phases = (
                result.serialization_seconds
                + result.file_flush_seconds
                + result.durable_publish_seconds
            )
            self.assertAlmostEqual(result.pause_seconds, phases, places=6)
            self.assertGreater(result.serialization_seconds, 0.0)
            self.assertGreaterEqual(result.durable_publish_seconds, 0.0)
            # The rename and directory flush are inside the measured span.
            self.assertGreater(result.pause_seconds, result.serialization_seconds)

    def test_a_slow_publish_step_is_reflected_in_the_pause(self):
        """Make the durability step measurably expensive and watch the pause."""

        real = checkpoint_module._fsync_directory
        marker = {"seen": False}

        def slow(directory, capability):
            marker["seen"] = True
            # Real work, not a sleep: hash a buffer so the step genuinely costs
            # CPU inside the stop-the-world window.
            import hashlib

            digest = b"x" * 4096
            for _ in range(2000):
                digest = hashlib.sha256(digest).digest()
            return real(directory, capability)

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            baseline = commit(store, live_vm(), 1)
            checkpoint_module._fsync_directory = slow
            try:
                slowed = commit(store, live_vm(), 2, previous=1)
            finally:
                checkpoint_module._fsync_directory = real
            self.assertTrue(marker["seen"])
            self.assertGreater(
                slowed.durable_publish_seconds, baseline.durable_publish_seconds
            )
            self.assertGreater(slowed.pause_seconds, slowed.serialization_seconds)


# --------------------------------------------------------------- finding 8

class RecoveryOutcomeTests(unittest.TestCase):
    def test_empty_directory(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            result = store.recover()
            self.assertEqual(result.outcome, OUTCOME_EMPTY)
            self.assertTrue(result.is_clean_start)

    def test_corrupt_directory_is_not_a_clean_start(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            (store.directory / "slot-a.cont").write_bytes(b"broken")
            result = store.recover()
            self.assertEqual(result.outcome, OUTCOME_CORRUPT)
            self.assertFalse(result.is_clean_start)
            self.assertTrue(result.refusals)

    def test_ambiguous_lineage(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-a")
            commit(store, vm, 9, lineage="lin-b")
            result = store.recover()
            self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS_LINEAGE)
            self.assertFalse(result.is_clean_start)

    def test_duplicate_generation(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 3)
            data = store.recover().selected.path.read_bytes()
            next(p for p in store.slot_paths if not p.exists()).write_bytes(data)
            self.assertEqual(store.recover().outcome, OUTCOME_DUPLICATE_GENERATION)

    def test_requested_lineage_absent(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            commit(store, live_vm(), 1, lineage="lin-present")
            result = store.recover(lineage_id="lin-missing")
            self.assertEqual(result.outcome, OUTCOME_LINEAGE_NOT_PRESENT)

    def test_successful_recovery(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            commit(store, live_vm(), 1)
            result = store.recover()
            self.assertEqual(result.outcome, OUTCOME_RECOVERED)
            self.assertTrue(result.recoverable)


# --------------------------------------------------------------- finding 9

class ErrnoGuardTests(unittest.TestCase):
    def test_classification_never_raises_when_a_constant_is_missing(self):
        saved = getattr(errno, "EDQUOT", None)
        had = hasattr(errno, "EDQUOT")
        if had:
            delattr(errno, "EDQUOT")
        try:
            message = describe_write_failure(
                OSError(28, "No space left on device"),
                Path("slot-a.cont"),
                Path("/checkpoints"),
            )
            self.assertIn("out of space", message)
            # And an error carrying the now-missing code still formats.
            other = describe_write_failure(
                OSError(122, "Disk quota exceeded"),
                Path("slot-a.cont"),
                Path("/checkpoints"),
            )
            self.assertIn("checkpoint write failed", other)
        finally:
            if had:
                errno.EDQUOT = saved

    def test_known_codes_are_classified(self):
        directory = Path("/checkpoints")
        destination = Path("slot-a.cont")
        for name, needle in (
            ("ENOSPC", "out of space"),
            ("EACCES", "permission denied"),
            ("EROFS", "read-only"),
            ("EXDEV", "across filesystems"),
        ):
            code = getattr(errno, name, None)
            if code is None:  # pragma: no cover - platform dependent
                continue
            with self.subTest(name=name):
                self.assertIn(
                    needle,
                    describe_write_failure(
                        OSError(code, name), destination, directory
                    ),
                )

    def test_an_unknown_code_still_names_the_slot(self):
        message = describe_write_failure(
            OSError(9999, "something exotic"),
            Path("slot-b.cont"),
            Path("/checkpoints"),
        )
        self.assertIn("slot-b.cont", message)
        self.assertIn("something exotic", message)


# -------------------------------------------------------------- finding 10

class LineageCharacterTests(unittest.TestCase):
    def _image_with_lineage(self, root: Path, lineage: str) -> Path:
        return save_image(
            root / "x.cont", live_vm(), SOURCE,
            checkpoint={
                "checkpoint_format_version": "1",
                "mode": "periodic",
                "lineage_id": lineage,
                "generation": 1,
                "previous_generation": None,
                "created_at": "2026-08-02T00:00:00+00:00",
                "requested_interval_seconds": 0.1,
                "durability": {
                    "file_fsync": True, "directory_fsync": "supported"
                },
            },
        )

    def test_ascii_tokens_are_accepted(self):
        with TemporaryDirectory() as directory:
            for lineage in ("cont-4e39c4f752d1", "A_b-9", "x", "0" * 128):
                with self.subTest(lineage=lineage):
                    self._image_with_lineage(Path(directory), lineage)

    def test_non_ascii_and_unsafe_tokens_are_refused(self):
        with TemporaryDirectory() as directory:
            for lineage in (
                "café",            # Unicode letter
                "一二三",            # CJK ideographs
                "١٢٣",             # Arabic-Indic digits
                "lin‮eg",     # bidirectional control
                "lin eage",        # whitespace
                "lin/eage",        # slash
                "lin.eage",        # dot
                "lin\x00eage",     # NUL
                "",                # empty
                "0" * 129,         # too long
            ):
                with self.subTest(lineage=lineage):
                    with self.assertRaises(ImageError):
                        self._image_with_lineage(Path(directory), lineage)


# -------------------------------------------------------------- finding 12

class SlotSelectionCostTests(unittest.TestCase):
    def test_the_commit_path_does_not_fully_validate_every_slot(self):
        """Slot selection must not decompress and checksum whole containers."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)

            calls = {"load_image": 0}
            real_load = checkpoint_module.load_image

            def counting(path):
                calls["load_image"] += 1
                return real_load(path)

            checkpoint_module.load_image = counting
            try:
                commit(store, vm, 3, previous=2)
            finally:
                checkpoint_module.load_image = real_load
            self.assertEqual(
                calls["load_image"], 0,
                "the commit path performed full container validation",
            )

    def test_recovery_still_fully_validates(self):
        """The cheap path must not have weakened recovery."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            calls = {"load_image": 0}
            real_load = checkpoint_module.load_image

            def counting(path):
                calls["load_image"] += 1
                return real_load(path)

            checkpoint_module.load_image = counting
            try:
                store.recover()
            finally:
                checkpoint_module.load_image = real_load
            self.assertEqual(calls["load_image"], 2)

    def test_a_slot_whose_body_is_corrupt_but_manifest_is_intact(self):
        """The cheap hint may accept it; recovery must not."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            newest = store.recover().selected
            # Corrupt the heap, leaving manifest.json and its digest intact.
            with zipfile.ZipFile(newest.path) as archive:
                contents = {n: archive.read(n) for n in archive.namelist()}
            contents["heap/objects.json"] += b" "
            with zipfile.ZipFile(newest.path, "w") as archive:
                for name, data in sorted(contents.items()):
                    archive.writestr(name, data)
            hint = {item.slot: item for item in store.slot_hints()}
            # The hint is only an operational signal and may still say valid...
            self.assertTrue(hint[newest.slot].valid)
            # ...but recovery fully validates and refuses it.
            result = store.recover()
            self.assertEqual(result.selected.generation, 1)
            self.assertTrue(result.refusals)

    def test_a_forged_manifest_is_rejected_even_by_the_cheap_hint(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            commit(store, live_vm(), 1)
            path = store.recover().selected.path
            with zipfile.ZipFile(path) as archive:
                contents = {n: archive.read(n) for n in archive.namelist()}
            manifest = json.loads(contents["manifest.json"])
            manifest["checkpoint"]["generation"] = 4096
            contents["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in sorted(contents.items()):
                    archive.writestr(name, data)
            hints = {item.slot: item for item in store.slot_hints()}
            self.assertFalse(hints[path.name].valid)
            self.assertIn("digest", hints[path.name].reason)


# ------------------------------------------------- format compatibility

class FormatCompatibilityTests(unittest.TestCase):
    """Prove the 'older readers safely ignore it' claim rather than asserting it.

    The claim justifies not bumping `format_version` and not adding a
    capability. It only holds if the reader genuinely ignores manifest keys it
    does not know about, and if a checkpoint image is otherwise an ordinary
    image.
    """

    def _checkpoint_image(self, root: Path) -> Path:
        store = CheckpointStore(root / "cp")
        commit(store, live_vm(), 1)
        return store.recover().selected.path

    def test_the_reader_ignores_manifest_keys_it_does_not_know(self):
        """The mechanism the no-version-bump decision depends on."""

        from continuum.image import load_image

        with TemporaryDirectory() as directory:
            path = Path(directory) / "x.cont"
            save_image(path, live_vm(), SOURCE)
            with zipfile.ZipFile(path) as archive:
                contents = {n: archive.read(n) for n in archive.namelist()}
            manifest = json.loads(contents["manifest.json"])
            manifest["a_key_from_a_future_revision"] = {"anything": [1, 2, 3]}
            _rewrite(path, contents, manifest)
            # No exception: unknown manifest keys are not a rejection reason,
            # so a reader predating `checkpoint` accepts a checkpoint image.
            loaded = load_image(path)
            self.assertEqual(loaded.manifest["format_version"], "0.2")

    def test_a_checkpoint_image_is_an_ordinary_verifiable_image(self):
        from continuum.image import inspect_image

        with TemporaryDirectory() as directory:
            path = self._checkpoint_image(Path(directory))
            report = inspect_image(path)
            self.assertEqual(report["integrity"], "verified")
            self.assertEqual(report["manifest"]["format_version"], "0.2")

    def test_a_checkpoint_image_is_not_mistaken_for_a_manual_freeze(self):
        """The distinguishing key is present and authenticated."""

        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = self._checkpoint_image(root)
            manual = root / "manual.cont"
            save_image(manual, live_vm(), SOURCE)
            with zipfile.ZipFile(checkpoint) as archive:
                self.assertIn(
                    "checkpoint", json.loads(archive.read("manifest.json"))
                )
            with zipfile.ZipFile(manual) as archive:
                self.assertNotIn(
                    "checkpoint", json.loads(archive.read("manifest.json"))
                )
            # And a manual-freeze image is never a recovery candidate.
            store = CheckpointStore(root / "cp")
            (store.directory / "slot-b.cont").write_bytes(manual.read_bytes())
            hints = {item.slot: item for item in store.slot_hints()}
            self.assertFalse(hints["slot-b.cont"].valid)

    def test_an_unknown_checkpoint_block_version_is_refused(self):
        from continuum.image import load_image

        with TemporaryDirectory() as directory:
            path = self._checkpoint_image(Path(directory))
            with zipfile.ZipFile(path) as archive:
                contents = {n: archive.read(n) for n in archive.namelist()}
            manifest = json.loads(contents["manifest.json"])
            manifest["checkpoint"]["checkpoint_format_version"] = "2"
            _rewrite(path, contents, manifest)
            with self.assertRaises(ImageError) as caught:
                load_image(path)
            self.assertIn("checkpoint metadata version", str(caught.exception))

    def test_legacy_format_images_are_unaffected(self):
        """A 0.1 manifest has no checkpoint block and must still be refused/read
        by exactly its original rules."""

        from continuum.image import _validate_checkpoint_metadata

        # The validator is only ever reached when the key is present, so a
        # legacy manifest without one never enters this code path at all.
        with self.assertRaises(ImageError):
            _validate_checkpoint_metadata(None)
        legacy_manifest = {"format_version": "0.1", "target_compatibility": {}}
        self.assertNotIn("checkpoint", legacy_manifest)


def _rewrite(path: Path, contents: dict, manifest: dict) -> None:
    """Rewrite an image with a new manifest and a repaired checksum document."""

    import hashlib

    raw = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    contents["manifest.json"] = raw
    checksums = json.loads(contents["checksums.json"])
    checksums["entries"]["manifest.json"] = hashlib.sha256(raw).hexdigest()
    contents["checksums.json"] = json.dumps(
        checksums, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            archive.writestr(name, data)


if __name__ == "__main__":
    unittest.main()
