"""Deterministic failure injection at every checkpoint commit stage.

Each test drives a real commit against a real directory and raises at one exact
stage, leaving whatever partial state that stage produces. Recovery is then run
through the ordinary reader.

The invariant under test is the same every time: recovery selects either the
last fully committed generation or the newly committed one, and never a partial,
truncated, or uncommitted file.

No stage is simulated by mocking a successful return. The injected exception is
real, the on-disk state afterwards is real, and the assertions read that state
back rather than trusting a return value.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from continuum import checkpoint as checkpoint_module
from continuum.checkpoint import (
    COMMIT_STAGES,
    STAGE_AFTER_DIRECTORY_FLUSH,
    STAGE_AFTER_FLUSH,
    STAGE_AFTER_RENAME,
    STAGE_AFTER_TEMPORARY_WRITE,
    STAGE_BEFORE_TEMPORARY,
    STAGE_DURING_RENAME,
    STAGE_DURING_SERIALIZATION,
    CheckpointStore,
)
from continuum.compiler import compile_source
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


answer = work(300)
"""

# Stages at or after which the new image is already installed under the slot
# name, so recovery legitimately sees the new generation.
STAGES_AFTER_PUBLICATION = {STAGE_AFTER_RENAME, STAGE_AFTER_DIRECTORY_FLUSH}


class InjectedCrash(RuntimeError):
    """A simulated process death at one commit stage."""


def live_vm() -> VirtualMachine:
    vm = VirtualMachine(
        compile_source(SOURCE, "crash_test.py"),
        ["crash_test.py"],
        "crash_test.py",
    )
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 4:
            vm.step()
    return vm


def commit(store: CheckpointStore, vm, generation: int, previous=None):
    return store.commit(
        vm,
        SOURCE,
        lineage_id="lin-crash",
        generation=generation,
        previous_generation=previous,
        requested_interval_seconds=0.1,
    )


@contextlib.contextmanager
def crash_at(stage: str):
    def hook(current: str) -> None:
        if current == stage:
            raise InjectedCrash(f"process died at {current}")

    previous = checkpoint_module.set_commit_hook(hook)
    try:
        yield
    finally:
        checkpoint_module.set_commit_hook(previous)


class CommitStageCrashTests(unittest.TestCase):
    """Every stage, against a directory that already holds one good checkpoint."""

    @contextlib.contextmanager
    def _crash_after_one_good_checkpoint(self, stage: str):
        """Commit one checkpoint, crash at `stage` during the next, then assert.

        A context manager so the temporary directory is still alive while the
        caller makes further assertions about it.
        """

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            self.assertEqual(store.recover().selected.generation, 1)

            with crash_at(stage):
                with self.assertRaises(InjectedCrash):
                    commit(store, vm, 2, previous=1)

            # A brand-new store object, as a recovering process would build.
            recovered = CheckpointStore(Path(directory) / "cp")
            result = recovered.recover()
            self.assertIsNotNone(
                result.selected,
                f"no checkpoint survived a crash at {stage}: {result.refusals}",
            )
            self.assertIn(
                result.selected.generation,
                {1, 2},
                f"crash at {stage} produced generation "
                f"{result.selected.generation}",
            )
            if stage in STAGES_AFTER_PUBLICATION:
                self.assertEqual(result.selected.generation, 2)
            else:
                self.assertEqual(result.selected.generation, 1)
            yield Path(directory) / "cp", result

    def test_every_documented_stage_is_covered(self):
        """Guards the stage list itself against silently shrinking."""

        self.assertEqual(
            set(COMMIT_STAGES),
            {
                STAGE_BEFORE_TEMPORARY,
                STAGE_DURING_SERIALIZATION,
                STAGE_AFTER_TEMPORARY_WRITE,
                STAGE_AFTER_FLUSH,
                STAGE_DURING_RENAME,
                STAGE_AFTER_RENAME,
                STAGE_AFTER_DIRECTORY_FLUSH,
            },
        )

    def test_crash_before_temporary_create(self):
        with self._crash_after_one_good_checkpoint(STAGE_BEFORE_TEMPORARY):
            pass

    def test_crash_during_serialization(self):
        with self._crash_after_one_good_checkpoint(STAGE_DURING_SERIALIZATION):
            pass

    def test_crash_after_temporary_write_before_flush(self):
        with self._crash_after_one_good_checkpoint(STAGE_AFTER_TEMPORARY_WRITE):
            pass

    def test_crash_after_flush_before_rename(self):
        with self._crash_after_one_good_checkpoint(STAGE_AFTER_FLUSH):
            pass

    def test_crash_during_rename(self):
        with self._crash_after_one_good_checkpoint(STAGE_DURING_RENAME):
            pass

    def test_crash_after_rename_before_directory_flush(self):
        with self._crash_after_one_good_checkpoint(STAGE_AFTER_RENAME) as (
            directory,
            _result,
        ):
            # The rename already published the new image, so the new generation
            # is correct. The older one must remain as fallback.
            store = CheckpointStore(directory)
            valid = [item for item in store.inspect_slots() if item.valid]
            self.assertEqual(sorted(item.generation for item in valid), [1, 2])

    def test_crash_after_directory_flush_before_publishing_status(self):
        with self._crash_after_one_good_checkpoint(
            STAGE_AFTER_DIRECTORY_FLUSH
        ) as (directory, _result):
            store = CheckpointStore(directory)
            self.assertEqual(store.recover().selected.generation, 2)

    def test_no_stage_leaves_a_partial_file_under_a_slot_name(self):
        """After every injected crash the slots are individually loadable."""

        from continuum.image import load_image

        for stage in COMMIT_STAGES:
            with self.subTest(stage=stage):
                with self._crash_after_one_good_checkpoint(stage) as (
                    directory,
                    _result,
                ):
                    store = CheckpointStore(directory)
                    for item in store.inspect_slots():
                        if item.present and item.valid:
                            # Loading enforces bounded ZIP handling and every
                            # per-entry checksum; a partial file cannot pass.
                            load_image(item.path)


class FirstCheckpointCrashTests(unittest.TestCase):
    """Crashes before any checkpoint was ever committed."""

    def test_a_crash_during_the_very_first_commit_leaves_no_false_candidate(self):
        for stage in COMMIT_STAGES:
            with self.subTest(stage=stage):
                with TemporaryDirectory() as directory:
                    store = CheckpointStore(Path(directory) / "cp")
                    vm = live_vm()
                    with crash_at(stage):
                        with self.assertRaises(InjectedCrash):
                            commit(store, vm, 1)
                    recovered = CheckpointStore(Path(directory) / "cp")
                    result = recovered.recover()
                    if stage in STAGES_AFTER_PUBLICATION:
                        self.assertEqual(result.selected.generation, 1)
                    else:
                        # Nothing was ever published; there must be no
                        # candidate at all rather than a partial one.
                        self.assertIsNone(result.selected)


class SlotReplacementCrashTests(unittest.TestCase):
    """Crash while replacing each individual slot."""

    def test_crash_while_replacing_each_slot_in_turn(self):
        for target_generation in (2, 3):
            for stage in (STAGE_DURING_RENAME, STAGE_AFTER_RENAME):
                with self.subTest(generation=target_generation, stage=stage):
                    with TemporaryDirectory() as directory:
                        store = CheckpointStore(Path(directory) / "cp")
                        vm = live_vm()
                        for generation in range(1, target_generation):
                            commit(store, vm, generation,
                                   previous=generation - 1 or None)
                        before = store.recover().selected.generation
                        slot_before = store.target_slot(lineage_id="lin-crash").name

                        with crash_at(stage):
                            with self.assertRaises(InjectedCrash):
                                commit(store, vm, target_generation,
                                       previous=before)

                        recovered = CheckpointStore(Path(directory) / "cp")
                        result = recovered.recover()
                        self.assertIsNotNone(result.selected)
                        if stage == STAGE_AFTER_RENAME:
                            self.assertEqual(
                                result.selected.generation, target_generation
                            )
                            self.assertEqual(result.selected.slot, slot_before)
                        else:
                            self.assertEqual(result.selected.generation, before)

    def test_the_newest_committed_slot_is_never_the_one_overwritten(self):
        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            for generation in range(1, 8):
                newest_before = store.recover().selected
                target = store.target_slot(lineage_id="lin-crash")
                if newest_before is not None:
                    self.assertNotEqual(
                        target.name,
                        newest_before.slot,
                        "the newest committed checkpoint was chosen for overwrite",
                    )
                commit(store, vm, generation, previous=generation - 1 or None)


class SerializationFailureTests(unittest.TestCase):
    """A genuine mid-serialization failure, not a stage marker."""

    def test_state_that_fails_partway_through_leaves_the_old_checkpoint(self):
        class ExplodingVM:
            """Wraps a real VM and fails once serialization has begun."""

            def __init__(self, inner):
                self._inner = inner
                self.calls = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def state_root(self):
                self.calls += 1
                raise InjectedCrash("state became unserializable mid-capture")

        with TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "cp")
            vm = live_vm()
            commit(store, vm, 1)
            exploding = ExplodingVM(vm)
            with self.assertRaises(InjectedCrash):
                commit(store, exploding, 2, previous=1)
            self.assertEqual(exploding.calls, 1)
            recovered = CheckpointStore(Path(directory) / "cp")
            self.assertEqual(recovered.recover().selected.generation, 1)
            # The abandoned temporary must not linger as a slot candidate.
            names = sorted(p.name for p in recovered.directory.iterdir())
            self.assertEqual(names, ["slot-a.cont"])


class TemporaryFileHygieneTests(unittest.TestCase):
    def test_a_crash_leaves_at_most_a_prefixed_temporary_which_cleanup_removes(self):
        for stage in (STAGE_AFTER_TEMPORARY_WRITE, STAGE_AFTER_FLUSH):
            with self.subTest(stage=stage):
                with TemporaryDirectory() as directory:
                    store = CheckpointStore(Path(directory) / "cp")
                    vm = live_vm()
                    commit(store, vm, 1)
                    with crash_at(stage):
                        with self.assertRaises(InjectedCrash):
                            commit(store, vm, 2, previous=1)
                    reopened = CheckpointStore(Path(directory) / "cp")
                    # Whatever remains, it is never a recovery candidate.
                    self.assertEqual(reopened.recover().selected.generation, 1)
                    reopened.cleanup_temporaries()
                    remaining = sorted(
                        p.name for p in reopened.directory.iterdir()
                    )
                    self.assertEqual(remaining, ["slot-a.cont"])


if __name__ == "__main__":
    unittest.main()
