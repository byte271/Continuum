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
    SLOT_COUNT,
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


def _seed_extra_lineage(store: CheckpointStore, lineage: str) -> None:
    vm = VirtualMachine(
        compile_source(SOURCE, "cli_test.py"), ["cli_test.py"], "cli_test.py"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        while len(vm.frames) < 2 or vm.frames[-1].locals.get("index") != 3:
            vm.step()
    store.commit(
        vm, SOURCE, lineage_id=lineage, generation=9,
        previous_generation=None, requested_interval_seconds=0.25,
    )


def seeded_directory(
    root: Path, *, generations: int = 2, lineage: str = "lin-cli"
) -> CheckpointStore:
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
            lineage_id=lineage,
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
                "--checkpoint-slots", "2",
                "--checkpoint-failure", "terminate",
                "program.py",
                "extra",
            ]
        )
        self.assertEqual(args.checkpoint_dir, "cps")
        self.assertEqual(args.checkpoint_interval, "100ms")
        self.assertEqual(args.checkpoint_slots, SLOT_COUNT)
        self.assertEqual(args.checkpoint_failure, FAILURE_TERMINATE)
        self.assertEqual(args.arguments, ["extra"])

    def test_defaults_are_conservative(self):
        args = cli._parser().parse_args(["run", "program.py"])
        self.assertEqual(args.checkpoint_interval, "1s")
        self.assertEqual(args.checkpoint_slots, SLOT_COUNT)
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


class SlotCountTests(unittest.TestCase):
    def test_a_non_default_slot_count_is_refused_by_run(self):
        """`recover` cannot discover another count, so `run` must not offer one."""

        # A temporary directory rather than a relative "cps": the refusal
        # currently happens before CheckpointStore would mkdir, but relying on
        # argument evaluation order to keep a test side-effect free is fragile.
        with TemporaryDirectory() as directory:
            for value in ("1", "3", "8"):
                with self.subTest(value=value):
                    args = cli._parser().parse_args(
                        ["run", "--checkpoint-dir", str(Path(directory) / "cp"),
                         "--checkpoint-slots", value, "p.py"]
                    )
                    with self.assertRaises(ContinuumError) as caught:
                        cli.open_checkpoint_store(args)
                    self.assertIn("exactly 2", str(caught.exception))
            # Nothing was created by the refused invocations.
            self.assertFalse((Path(directory) / "cp").exists())

    def test_run_recover_and_checkpoints_agree_on_the_slot_count(self):
        with TemporaryDirectory() as directory:
            store = seeded_directory(Path(directory))
            self.assertEqual(store.slots, SLOT_COUNT)
            args = cli._parser().parse_args(
                ["checkpoints", str(store.directory), "--json"]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli._checkpoints(args)
            self.assertEqual(json.loads(output.getvalue())["slot_count"], SLOT_COUNT)


class RecoveryRefusalCliTests(unittest.TestCase):
    """--recover-latest must never silently restart from the beginning."""

    def _run_args(self, directory: Path, program: Path):
        return cli._parser().parse_args(
            ["run", "--checkpoint-dir", str(directory), "--recover-latest",
             str(program)]
        )

    def _program(self, root: Path) -> Path:
        path = root / "prog.py"
        path.write_text(SOURCE, encoding="utf-8")
        return path

    def test_corrupt_directory_refuses_instead_of_starting_over(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoints = root / "cp"
            checkpoints.mkdir()
            (checkpoints / "slot-a.cont").write_bytes(b"broken")
            stderr = io.StringIO()
            args = self._run_args(checkpoints, self._program(root))
            with contextlib.redirect_stderr(stderr):
                store, _interval = cli.open_checkpoint_store(args)
                with self.assertRaises(ContinuumError) as caught:
                    cli.resolve_recovery(store, True)
            message = str(caught.exception)
            self.assertIn("refusing to start from", message)
            self.assertIn("corrupt", message)
            self.assertIn("refused", stderr.getvalue())

    def test_ambiguous_lineage_refuses_instead_of_starting_over(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = seeded_directory(root, generations=1, lineage="lin-a")
            _seed_extra_lineage(store, "lin-b")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ContinuumError) as caught:
                    cli.resolve_recovery(store, True)
            self.assertIn("ambiguous-lineage", str(caught.exception))

    def test_an_empty_directory_starts_fresh_without_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "cp"
            empty.mkdir()
            stderr = io.StringIO()
            args = self._run_args(empty, self._program(root))
            with contextlib.redirect_stderr(stderr):
                store, _interval = cli.open_checkpoint_store(args)
                self.assertIsNone(cli.resolve_recovery(store, True))
            self.assertIn("starting from the beginning", stderr.getvalue())

    def test_a_foreign_lineage_directory_refuses_a_fresh_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = seeded_directory(root, generations=1, lineage="lin-old")
            with self.assertRaises(ContinuumError) as caught:
                store.claim_for_new_lineage("lin-brand-new")
            message = str(caught.exception)
            self.assertIn("already holds committed checkpoints", message)
            self.assertIn("lin-old", message)
            # Nothing was destroyed by the refusal.
            self.assertEqual(store.recover().selected.generation, 1)


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



class BenchmarkReportSchemaTests(unittest.TestCase):
    """The CI summary step must not read keys the benchmark stopped emitting.

    Renaming a report field twice broke the `checkpoint benchmark` job at a
    point where nothing else would have caught it: the benchmark refuses to run
    on an unverified interpreter, so the failure only appeared in CI. This
    compares what the workflow reads against what the module declares it
    writes, which needs neither a verified interpreter nor a real measurement.
    """

    def _workflow_text(self) -> str:
        path = (
            Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "rolling-checkpoints.yml"
        )
        return path.read_text(encoding="utf-8")

    def test_declared_keys_match_what_the_module_actually_emits(self):
        import ast

        source = (
            Path(__file__).resolve().parents[1]
            / "benchmarks" / "measure_checkpoints.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        emitted = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_run_workload"
            ):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and isinstance(
                        inner.value, ast.Dict
                    ):
                        emitted = {
                            key.value
                            for key in inner.value.keys
                            if isinstance(key, ast.Constant)
                        }
        self.assertIsNotNone(emitted, "could not find the report dict")
        from benchmarks.measure_checkpoints import INTERVAL_REPORT_KEYS

        # `recovery` is attached by main() after the workload runs, so it is in
        # the report but not in this function's return literal.
        self.assertEqual(emitted | {"recovery"}, set(INTERVAL_REPORT_KEYS))

    def test_the_workflow_only_reads_keys_the_report_contains(self):
        import re

        from benchmarks.measure_checkpoints import INTERVAL_REPORT_KEYS

        read = set(re.findall(r"measured\[['\"]([a-z_]+)['\"]\]", self._workflow_text()))
        self.assertTrue(read, "found no report reads in the workflow")
        missing = read - set(INTERVAL_REPORT_KEYS)
        self.assertEqual(
            missing, set(),
            f"the workflow reads keys the benchmark does not emit: {sorted(missing)}",
        )

if __name__ == "__main__":
    unittest.main()
