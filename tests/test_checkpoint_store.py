"""Rolling checkpoint store: rotation, generation, selection, and durability.

These tests exercise the real store against a real temporary directory. Nothing
here mocks a successful commit: every assertion about what survives is made by
reading the directory back through the ordinary image reader.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from continuum.checkpoint import (
    DIRECTORY_FSYNC_SUPPORTED,
    DIRECTORY_FSYNC_UNSUPPORTED,
    FAILURE_CONTINUE,
    FAILURE_TERMINATE,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CheckpointScheduler,
    CheckpointStore,
    parse_interval,
    parse_slots,
)
from continuum.compiler import compile_source
from continuum.errors import CheckpointError
from continuum.image import CHECKPOINT_METADATA_VERSION
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


def live_vm(callback=None) -> VirtualMachine:
    """A VM stopped partway through, with output suppressed."""

    vm = VirtualMachine(
        compile_source(SOURCE, "checkpoint_test.py"),
        ["checkpoint_test.py"],
        "checkpoint_test.py",
        safe_point_callback=callback,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 5:
            vm.step()
    return vm


def commit(store: CheckpointStore, vm, generation: int, *, lineage="lin-test",
           previous=None):
    return store.commit(
        vm,
        SOURCE,
        lineage_id=lineage,
        generation=generation,
        previous_generation=previous,
        requested_interval_seconds=0.1,
    )


class IntervalParsingTests(unittest.TestCase):
    def test_supported_forms(self):
        self.assertAlmostEqual(parse_interval("100ms"), 0.1)
        self.assertAlmostEqual(parse_interval("1s"), 1.0)
        self.assertAlmostEqual(parse_interval("5s"), 5.0)
        self.assertAlmostEqual(parse_interval("2.5s"), 2.5)
        self.assertAlmostEqual(parse_interval("1m"), 60.0)
        self.assertAlmostEqual(parse_interval("3"), 3.0)
        self.assertAlmostEqual(parse_interval("  100MS "), 0.1)

    def test_rejected_forms(self):
        for value in (
            "", "   ", "0", "0s", "0ms", "-1s", "abc", "1h", "1 s", "s", "1.2.3s",
            "1e3s", "NaN", "inf", "--1s", "1s1s", "+1s",
        ):
            with self.subTest(value=value):
                with self.assertRaises(CheckpointError):
                    parse_interval(value)

    def test_non_string_is_refused(self):
        for value in (None, 1.0, 100, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(CheckpointError):
                    parse_interval(value)

    def test_range_boundaries(self):
        self.assertAlmostEqual(parse_interval("1ms"), MIN_INTERVAL_SECONDS)
        self.assertAlmostEqual(
            parse_interval(f"{int(MAX_INTERVAL_SECONDS)}s"), MAX_INTERVAL_SECONDS
        )
        with self.assertRaises(CheckpointError):
            parse_interval("0.5ms")
        with self.assertRaises(CheckpointError):
            parse_interval(f"{int(MAX_INTERVAL_SECONDS) + 1}s")

    def test_slot_counts(self):
        self.assertEqual(parse_slots(2), 2)
        self.assertEqual(parse_slots(3), 3)
        for value in (0, 1, -1, 9, True, 2.0, "2"):
            with self.subTest(value=value):
                with self.assertRaises(CheckpointError):
                    parse_slots(value)


class RotationTests(unittest.TestCase):
    def test_generations_increase_and_rotate_between_two_slots(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            seen = []
            for generation in range(1, 7):
                result = commit(store, vm, generation,
                                previous=generation - 1 or None)
                seen.append((result.generation, result.slot))
            self.assertEqual([item[0] for item in seen], [1, 2, 3, 4, 5, 6])
            # Strict alternation: the slot being written is always the older one.
            self.assertEqual(
                [item[1] for item in seen],
                ["slot-a.cont", "slot-b.cont"] * 3,
            )
            files = sorted(p.name for p in store.directory.iterdir())
            self.assertEqual(files, ["slot-a.cont", "slot-b.cont"])

    def test_exactly_two_committed_slots_remain(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            for generation in range(1, 11):
                commit(store, vm, generation, previous=generation - 1 or None)
            inspections = store.inspect_slots()
            self.assertEqual(len(inspections), 2)
            self.assertTrue(all(item.valid for item in inspections))
            self.assertEqual(
                sorted(item.generation for item in inspections), [9, 10]
            )

    def test_the_previous_slot_survives_the_whole_next_commit(self):
        """The old checkpoint must be readable at every instant of the new one."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            observed = []

            def watch(stage: str) -> None:
                result = store.recover()
                observed.append(
                    (stage, result.selected.generation if result.selected else None)
                )

            from continuum import checkpoint as checkpoint_module

            previous = checkpoint_module.set_commit_hook(watch)
            try:
                commit(store, vm, 2, previous=1)
            finally:
                checkpoint_module.set_commit_hook(previous)
            # At every stage before the rename a valid checkpoint was readable.
            self.assertTrue(observed)
            for stage, generation in observed:
                with self.subTest(stage=stage):
                    self.assertIsNotNone(
                        generation, f"no valid checkpoint existed at {stage}"
                    )

    def test_selection_ignores_timestamps(self):
        """Generation, not mtime, decides. Make mtime disagree and re-check."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            newest = store.recover().selected
            self.assertEqual(newest.generation, 2)
            older = next(
                item for item in store.inspect_slots() if item.generation == 1
            )
            # Make the *older* generation look far newer on disk.
            os.utime(older.path, (2_000_000_000, 2_000_000_000))
            os.utime(newest.path, (1_000_000, 1_000_000))
            self.assertEqual(store.recover().selected.generation, 2)

    def test_conflicting_equal_generations_are_refused_deterministically(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            slots = store.inspect_slots()
            # Force a state the writer cannot produce: two slots, one generation.
            source_slot = next(item for item in slots if item.generation == 2)
            target_slot = next(item for item in slots if item.generation == 1)
            target_slot.path.write_bytes(source_slot.path.read_bytes())
            result = store.recover()
            self.assertIsNone(result.selected)
            self.assertTrue(
                any("more than one slot" in reason for reason in result.refusals),
                result.refusals,
            )


class RecoverySelectionTests(unittest.TestCase):
    def test_empty_directory_selects_nothing(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            result = store.recover()
            self.assertIsNone(result.selected)
            self.assertEqual(store.next_generation(), 1)

    def test_falls_back_when_the_newest_slot_is_truncated(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            newest = store.recover().selected
            data = newest.path.read_bytes()
            newest.path.write_bytes(data[: len(data) // 2])
            result = store.recover()
            self.assertIsNotNone(result.selected)
            self.assertEqual(result.selected.generation, 1)
            self.assertTrue(result.refusals)

    def test_falls_back_on_checksum_mismatch(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            newest = store.recover().selected
            _corrupt_entry(newest.path, "heap/objects.json")
            result = store.recover()
            self.assertEqual(result.selected.generation, 1)

    def test_falls_back_on_malformed_zip(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            store.recover().selected.path.write_bytes(b"not a zip at all")
            self.assertEqual(store.recover().selected.generation, 1)

    def test_both_slots_corrupt_selects_nothing(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            for item in store.inspect_slots():
                item.path.write_bytes(b"broken")
            result = store.recover()
            self.assertIsNone(result.selected)
            self.assertEqual(len(result.refusals), 2)

    def test_foreign_lineage_slot_is_refused_not_mixed(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-real")
            commit(store, vm, 2, lineage="lin-real", previous=1)
            # Overwrite the older slot with a much higher generation from an
            # unrelated session, as a stray copy into the directory would.
            store.commit(
                vm,
                SOURCE,
                lineage_id="lin-other",
                generation=99,
                previous_generation=None,
                requested_interval_seconds=0.1,
            )
            result = store.recover(lineage_id="lin-real")
            self.assertEqual(result.selected.generation, 2)
            self.assertTrue(
                any("lin-other" in reason for reason in result.refusals),
                result.refusals,
            )

    def test_ambiguous_lineages_refuse_rather_than_guess(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1, lineage="lin-a")
            commit(store, vm, 5, lineage="lin-b")
            result = store.recover()
            self.assertIsNone(result.selected)
            self.assertTrue(
                any("unrelated lineages" in reason for reason in result.refusals),
                result.refusals,
            )

    def test_an_image_without_checkpoint_metadata_is_not_a_candidate(self):
        """A manual freeze image dropped into a slot must not be selected."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            from continuum.image import save_image

            target = store.directory / "slot-b.cont"
            save_image(target, vm, SOURCE)  # no checkpoint= argument
            inspections = {item.slot: item for item in store.inspect_slots()}
            self.assertFalse(inspections["slot-b.cont"].valid)
            self.assertIn("no checkpoint metadata", inspections["slot-b.cont"].reason)
            self.assertEqual(store.recover().selected.generation, 1)

    def test_unverified_generation_field_is_refused(self):
        """Rewriting the generation without fixing checksums must not be trusted."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            commit(store, vm, 2, previous=1)
            older = min(store.inspect_slots(), key=lambda item: item.generation)
            _rewrite_manifest_generation(older.path, 4096)
            result = store.recover()
            # The forged slot fails its own integrity check, so generation 2
            # still wins and the forgery is reported rather than selected.
            self.assertEqual(result.selected.generation, 2)
            self.assertTrue(result.refusals)

    def test_external_metadata_claiming_a_missing_generation_is_ignored(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            (store.directory / "latest.json").write_text(
                json.dumps({"generation": 500, "slot": "slot-b.cont"}),
                encoding="utf-8",
            )
            # Selection consults only the images themselves.
            self.assertEqual(store.recover().selected.generation, 1)

    def test_stale_temporary_files_are_never_candidates_and_are_cleaned(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            stale = store.directory / ".checkpoint-deadbeef.tmp"
            stale.write_bytes(b"partial image bytes")
            self.assertEqual(store.recover().selected.generation, 1)
            removed = store.cleanup_temporaries()
            self.assertEqual(removed, [stale.name])
            self.assertFalse(stale.exists())
            # Committed slots are untouched by cleanup.
            self.assertEqual(store.recover().selected.generation, 1)

    def test_cleanup_leaves_unrelated_files_alone(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            keep = store.directory / "notes.txt"
            keep.write_text("keep me", encoding="utf-8")
            self.assertEqual(store.cleanup_temporaries(), [])
            self.assertTrue(keep.exists())


class DurabilityContractTests(unittest.TestCase):
    def test_probe_reports_a_known_state(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            self.assertIn(
                store.directory_fsync,
                {DIRECTORY_FSYNC_SUPPORTED, DIRECTORY_FSYNC_UNSUPPORTED},
            )
            if os.name == "nt":
                self.assertEqual(store.directory_fsync, DIRECTORY_FSYNC_UNSUPPORTED)
            else:
                # Every filesystem the test suite runs on supports this. If a
                # platform genuinely cannot, the weaker answer must be recorded
                # rather than the commit claiming durability it did not achieve.
                self.assertEqual(store.directory_fsync, DIRECTORY_FSYNC_SUPPORTED)

    def test_commit_records_the_achieved_durability_in_the_image(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            result = commit(store, vm, 1)
            self.assertEqual(result.directory_fsync, store.directory_fsync)
            self.assertEqual(
                result.durable, store.directory_fsync == DIRECTORY_FSYNC_SUPPORTED
            )
            item = store.recover().selected
            self.assertEqual(item.directory_fsync, store.directory_fsync)

    def test_checkpoint_metadata_is_covered_by_container_integrity(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            path = store.recover().selected.path
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                checksums = json.loads(archive.read("checksums.json"))
            self.assertEqual(
                manifest["checkpoint"]["checkpoint_format_version"],
                CHECKPOINT_METADATA_VERSION,
            )
            self.assertIn("manifest.json", checksums["entries"])


class SchedulerTests(unittest.TestCase):
    def test_only_fires_when_due_on_a_monotonic_clock(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [1000.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=lambda: now[0],
            )
            vm = live_vm()
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 0)
            now[0] += 0.999
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 0)
            now[0] += 0.002
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 1)

    def test_missed_ticks_coalesce_into_one(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [1000.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=0.1,
                clock=lambda: now[0],
            )
            vm = live_vm()
            # Jump far past many deadlines, as a 350ms commit against a 100ms
            # interval would. One checkpoint must result, not a backlog.
            now[0] += 5.0
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 1)
            self.assertGreater(scheduler.status.coalesced_ticks, 0)
            # And the next one is not immediately due again.
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 1)

    def test_scheduling_does_not_drift_when_commits_are_fast(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [0.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=lambda: now[0],
            )
            vm = live_vm()
            for tick in range(1, 5):
                now[0] = float(tick)
                scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 4)
            self.assertEqual(scheduler.status.coalesced_ticks, 0)

    def test_generations_are_monotonic_across_scheduler_commits(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [0.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=lambda: now[0],
            )
            vm = live_vm()
            for tick in range(1, 6):
                now[0] = float(tick)
                scheduler.on_safe_point(vm)
            generations = [item.generation for item in scheduler.history]
            self.assertEqual(generations, [1, 2, 3, 4, 5])
            self.assertEqual(store.recover().selected.generation, 5)

    def test_a_resumed_scheduler_continues_the_generation_sequence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cp"
            store = CheckpointStore(path)
            vm = live_vm()
            commit(store, vm, 1, lineage="lin")
            commit(store, vm, 2, lineage="lin", previous=1)
            reopened = CheckpointStore(path)
            scheduler = CheckpointScheduler(
                reopened, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=lambda: 10_000.0,
            )
            self.assertEqual(scheduler.generation, 2)
            scheduler.checkpoint(vm)
            self.assertEqual(scheduler.generation, 3)

    def test_stop_is_idempotent_and_leaves_the_session_intact(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [0.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                clock=lambda: now[0],
            )
            vm = live_vm()
            now[0] = 1.0
            scheduler.on_safe_point(vm)
            scheduler.stop()
            scheduler.stop()
            now[0] = 100.0
            scheduler.on_safe_point(vm)
            self.assertEqual(scheduler.status.commits, 1)
            self.assertFalse(scheduler.status.enabled)
            self.assertFalse(scheduler.status.writing)
            # The committed checkpoint is still valid after stopping.
            self.assertEqual(store.recover().selected.generation, 1)

    def test_failure_policy_continue_keeps_running_and_records_the_error(self):
        from continuum import checkpoint as checkpoint_module

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [0.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                failure_policy=FAILURE_CONTINUE, clock=lambda: now[0],
            )
            vm = live_vm()

            def fail(stage: str) -> None:
                if stage == "before-temporary-create":
                    raise OSError(28, "No space left on device")

            previous = checkpoint_module.set_commit_hook(fail)
            try:
                now[0] = 1.0
                scheduler.on_safe_point(vm)
            finally:
                checkpoint_module.set_commit_hook(previous)
            self.assertEqual(scheduler.status.commits, 0)
            self.assertEqual(scheduler.status.failures, 1)
            self.assertIn("space", scheduler.status.last_error.lower())
            self.assertFalse(scheduler.status.writing)

    def test_failure_policy_terminate_raises(self):
        from continuum import checkpoint as checkpoint_module

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            now = [0.0]
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                failure_policy=FAILURE_TERMINATE, clock=lambda: now[0],
            )
            vm = live_vm()

            def fail(stage: str) -> None:
                if stage == "before-temporary-create":
                    raise OSError(13, "Permission denied")

            previous = checkpoint_module.set_commit_hook(fail)
            try:
                now[0] = 1.0
                with self.assertRaises(CheckpointError):
                    scheduler.on_safe_point(vm)
            finally:
                checkpoint_module.set_commit_hook(previous)

    def test_unknown_failure_policy_is_refused(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            with self.assertRaises(CheckpointError):
                CheckpointScheduler(
                    store, SOURCE, lineage_id="lin", interval_seconds=1.0,
                    failure_policy="halt-and-catch-fire",
                )


class NonTerminatingExecutionTests(unittest.TestCase):
    def test_the_program_runs_to_completion_while_checkpointing(self):
        """The defining property: checkpointing must not end the program."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=MIN_INTERVAL_SECONDS,
            )
            vm = VirtualMachine(
                compile_source(SOURCE, "checkpoint_test.py"),
                ["checkpoint_test.py"],
                "checkpoint_test.py",
                safe_point_callback=scheduler.on_safe_point,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                vm.run()
            self.assertTrue(vm.completed)
            self.assertEqual(vm.globals["answer"], sum(range(400)))
            self.assertGreater(scheduler.status.commits, 1)

    def test_no_instruction_is_replayed_during_normal_execution(self):
        """Every STEP appears exactly once despite many checkpoints."""

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=MIN_INTERVAL_SECONDS,
            )
            vm = VirtualMachine(
                compile_source(SOURCE, "checkpoint_test.py"),
                ["checkpoint_test.py"],
                "checkpoint_test.py",
                safe_point_callback=scheduler.on_safe_point,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                vm.run()
            steps = [
                line for line in output.getvalue().splitlines()
                if line.startswith("STEP ")
            ]
            self.assertGreater(scheduler.status.commits, 1)
            self.assertEqual(len(steps), 400)
            self.assertEqual(len(set(steps)), 400)

    def test_a_checkpoint_never_produces_a_frozen_session(self):
        """Checkpointing must not raise FrozenExecution the way freeze does."""

        from continuum.errors import FrozenExecution

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            scheduler = CheckpointScheduler(
                store, SOURCE, lineage_id="lin", interval_seconds=MIN_INTERVAL_SECONDS,
            )
            vm = live_vm()
            try:
                scheduler.checkpoint(vm)
            except FrozenExecution:  # pragma: no cover - the failure we guard
                self.fail("a periodic checkpoint terminated the execution")
            self.assertEqual(scheduler.status.commits, 1)


def _corrupt_entry(path: Path, entry: str) -> None:
    """Rewrite one archive entry, leaving checksums.json stale."""

    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    contents[entry] = contents[entry] + b" "
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            archive.writestr(name, data)


def _rewrite_manifest_generation(path: Path, generation: int) -> None:
    """Forge a higher generation without repairing the integrity document."""

    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    manifest["checkpoint"]["generation"] = generation
    contents["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            archive.writestr(name, data)


if __name__ == "__main__":
    unittest.main()
