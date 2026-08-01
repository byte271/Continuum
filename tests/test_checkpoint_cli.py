"""CLI surface for rolling checkpoints, including backwards compatibility."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from continuum import cli
from continuum.checkpoint import (
    FAILURE_CONTINUE,
    FAILURE_TERMINATE,
    MIN_SLOTS,
    CheckpointStore,
)
from continuum.compiler import compile_source
from continuum.errors import ContinuumError
from continuum.vm import VirtualMachine

SOURCE = """
def work(limit):
    total = 0
    index = 0
    while index < limit:
        total = total + index
        index += 1
    return total


answer = work(50)
"""


def seeded_directory(root: Path, *, generations: int = 2) -> CheckpointStore:
    store = CheckpointStore(root / "cp")
    vm = VirtualMachine(
        compile_source(SOURCE, "cli_test.py"), ["cli_test.py"], "cli_test.py"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 3:
            vm.step()
    for generation in range(1, generations + 1):
        store.commit(
            vm,
            SOURCE,
            lineage_id="lin-cli",
            generation=generation,
            previous_generation=generation - 1 or None,
            requested_interval_seconds=0.25,
        )
    return store


class ParserBackwardsCompatibilityTests(unittest.TestCase):
    def test_run_without_checkpoint_options_is_unchanged(self):
        args = cli._parser().parse_args(["run", "program.py"])
        self.assertIsNone(args.checkpoint_dir)
        self.assertFalse(args.recover_latest)
        self.assertEqual(args.file_policy, "strict")
        self.assertEqual(args.arguments, [])

    def test_arguments_after_the_program_still_belong_to_the_program(self):
        """The REMAINDER convention is preserved: no existing invocation changes."""

        args = cli._parser().parse_args(
            ["run", "program.py", "--checkpoint-dir", "x", "-v"]
        )
        self.assertIsNone(args.checkpoint_dir)
        self.assertEqual(args.arguments, ["--checkpoint-dir", "x", "-v"])

    def test_checkpoint_options_precede_the_program(self):
        args = cli._parser().parse_args(
            [
                "run",
                "--checkpoint-dir", "cps",
                "--checkpoint-interval", "100ms",
                "--checkpoint-slots", "3",
                "--checkpoint-failure", "terminate",
                "program.py",
                "extra",
            ]
        )
        self.assertEqual(args.checkpoint_dir, "cps")
        self.assertEqual(args.checkpoint_interval, "100ms")
        self.assertEqual(args.checkpoint_slots, 3)
        self.assertEqual(args.checkpoint_failure, FAILURE_TERMINATE)
        self.assertEqual(args.arguments, ["extra"])

    def test_defaults_are_conservative(self):
        args = cli._parser().parse_args(["run", "program.py"])
        self.assertEqual(args.checkpoint_interval, "1s")
        self.assertEqual(args.checkpoint_slots, MIN_SLOTS)
        self.assertEqual(args.checkpoint_failure, FAILURE_CONTINUE)

    def test_every_pre_existing_command_still_parses(self):
        parser = cli._parser()
        for argv in (
            ["run", "p.py"],
            ["sessions"],
            ["freeze", "cont-1", "-o", "out.cont"],
            ["inspect", "image.cont"],
            ["verify", "image.cont"],
            ["resume", "image.cont"],
            ["plan-upgrade", "i.cont", "new.py", "-o", "plan.json"],
            ["inspect-upgrade", "plan.json"],
            ["verify-upgrade", "i.cont", "plan.json"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(parser.parse_args(argv))

    def test_recover_latest_requires_a_directory(self):
        args = cli._parser().parse_args(["run", "--recover-latest", "p.py"])
        with self.assertRaises(ContinuumError) as caught:
            cli._run(args)
        self.assertIn("--recover-latest requires", str(caught.exception))


class CheckpointsCommandTests(unittest.TestCase):
    def test_reports_active_and_fallback_slots(self):
        with TemporaryDirectory() as directory:
            store = seeded_directory(Path(directory))
            args = cli._parser().parse_args(
                ["checkpoints", str(store.directory), "--json"]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli._checkpoints(args), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["last_generation"], 2)
            self.assertEqual(report["lineage_id"], "lin-cli")
            self.assertEqual(report["slot_count"], 2)
            self.assertIsNotNone(report["active_slot"])
            self.assertIsNotNone(report["fallback_slot"])
            self.assertNotEqual(report["active_slot"], report["fallback_slot"])
            self.assertEqual(report["requested_interval_seconds"], 0.25)
            self.assertIn(report["directory_fsync"], {"supported", "unsupported-on-platform"})

    def test_human_output_names_every_documented_field(self):
        with TemporaryDirectory() as directory:
            store = seeded_directory(Path(directory))
            args = cli._parser().parse_args(["checkpoints", str(store.directory)])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli._checkpoints(args)
            text = output.getvalue()
            for label in (
                "Checkpoint directory:",
                "Configured slots:",
                "Lineage:",
                "Active slot:",
                "Fallback slot:",
                "Last committed generation:",
                "Last committed at:",
                "Requested interval:",
                "Directory flush:",
            ):
                with self.subTest(label=label):
                    self.assertIn(label, text)

    def test_an_empty_directory_reports_no_checkpoint_rather_than_failing(self):
        with TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            args = cli._parser().parse_args(["checkpoints", str(empty), "--json"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli._checkpoints(args), 0)
            report = json.loads(output.getvalue())
            self.assertIsNone(report["last_generation"])
            self.assertIsNone(report["active_slot"])

    def test_a_corrupt_slot_is_reported_with_its_reason(self):
        with TemporaryDirectory() as directory:
            store = seeded_directory(Path(directory))
            newest = store.recover().selected
            newest.path.write_bytes(b"broken")
            args = cli._parser().parse_args(
                ["checkpoints", str(store.directory), "--json"]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli._checkpoints(args)
            report = json.loads(output.getvalue())
            self.assertEqual(report["last_generation"], 1)
            broken = [item for item in report["slots"] if not item["valid"]]
            self.assertTrue(broken)
            self.assertTrue(broken[0]["reason"])


if __name__ == "__main__":
    unittest.main()
