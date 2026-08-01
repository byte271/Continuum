"""End-to-end crash recovery against a real, forcefully killed process.

A long-running program is launched under `continuum run --checkpoint-dir`,
allowed to commit several checkpoints, and then killed without warning --
SIGKILL on POSIX, TerminateProcess via Popen.kill() on Windows. Neither gives
the process a chance to flush anything, so whatever recovery finds is what the
durable commit protocol actually put on disk.

Recovery then runs in a genuinely separate process, so nothing in-memory from
the dead one can influence the result.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY = Path(__file__).resolve().parents[1]

# Each iteration prints a unique, ordered marker. The markers are the oracle:
# they show exactly which work survived, which was replayed, and which was lost.
PROGRAM = """
def work(limit):
    total = 0
    index = 0
    while index < limit:
        total = total + index
        print(f"TICK {index}", flush=True)
        index += 1
    return total


answer = work(100000)
print(f"FINAL {answer}", flush=True)
"""


def continuum(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "continuum", *arguments]


def child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPOSITORY)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def wait_for_generation(
    directory: Path, minimum: int, process: subprocess.Popen,
    timeout: float = 120.0,
) -> int:
    """Block until the directory holds at least generation `minimum`.

    A deterministic barrier on committed on-disk state, not a fixed sleep: it
    proceeds the moment the requirement is met, and fails loudly with the
    child's diagnostics if the child dies or the requirement is never met.
    """

    from continuum.checkpoint import CheckpointStore

    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"the source exited early with code {process.returncode}"
            )
        if directory.exists():
            selected = CheckpointStore(directory).recover().selected
            last = selected.generation if selected else 0
            if last >= minimum:
                return last
        time.sleep(0.02)
    raise AssertionError(
        f"only reached generation {last} of {minimum} within {timeout}s"
    )


def kill_hard(process: subprocess.Popen) -> None:
    """Terminate without any chance to clean up, on either platform."""

    if os.name == "nt":
        process.kill()  # TerminateProcess; no handler runs
    else:
        os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=30)


def assert_dead(process: subprocess.Popen) -> None:
    """The source must be truly gone before recovery starts."""

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - only on a stuck process
        raise AssertionError("the source process did not die")
    if os.name != "nt":
        try:
            os.kill(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:  # pragma: no cover - reaped and reused
            return
        raise AssertionError(f"process {process.pid} is still alive")


class ProcessCrashRecoveryTests(unittest.TestCase):
    maxDiff = None

    def test_sigkill_then_recover_from_the_newest_valid_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "ticker.py"
            program.write_text(PROGRAM, encoding="utf-8")
            checkpoints = root / "checkpoints"

            out_path = root / "source.out"
            err_path = root / "source.err"
            # Files rather than pipes: this program outpaces any reader, and a
            # full pipe would block it mid-run, stalling the very checkpoints
            # under test.
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                source = subprocess.Popen(
                    continuum(
                        "run",
                        # Options precede the program name: everything after it
                        # is argparse.REMAINDER and belongs to the program.
                        "--checkpoint-dir", str(checkpoints),
                        "--checkpoint-interval", "50ms",
                        str(program),
                    ),
                    cwd=REPOSITORY,
                    env=child_environment(),
                    stdout=out,
                    stderr=err,
                )
                try:
                    reached = wait_for_generation(checkpoints, 3, source)
                    self.assertGreaterEqual(reached, 3)
                    kill_hard(source)
                finally:
                    if source.poll() is None:  # pragma: no cover - cleanup only
                        source.kill()
                        source.wait(timeout=30)
            assert_dead(source)
            before = [
                line
                for line in out_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("TICK ")
            ]
            self.assertTrue(before, err_path.read_text(encoding="utf-8"))

            # What the durable protocol actually left behind.
            report = json.loads(
                subprocess.run(
                    continuum("checkpoints", str(checkpoints), "--json"),
                    cwd=REPOSITORY, env=child_environment(),
                    capture_output=True, text=True, check=True,
                ).stdout
            )
            selected_generation = report["last_generation"]
            self.assertIsNotNone(selected_generation)
            valid = [item for item in report["slots"] if item["valid"]]
            self.assertTrue(valid)
            self.assertEqual(
                selected_generation, max(item["generation"] for item in valid)
            )

            recovered = subprocess.run(
                continuum("recover", str(checkpoints)),
                cwd=REPOSITORY,
                env=child_environment(),
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                f"Recovering generation {selected_generation}", recovered.stderr
            )
            after = [
                line for line in recovered.stdout.splitlines()
                if line.startswith("TICK ")
            ]
            self.assertTrue(after)

            # The saved program counter is honoured: recovery does not restart
            # the program from its entry point.
            self.assertNotEqual(after[0], "TICK 0")
            first_after = int(after[0].split()[1])
            self.assertGreater(first_after, 0)

            # Every tick before the recovery point happened exactly once across
            # both processes: no committed work was replayed and none was lost.
            union = before + after
            covered = sorted(int(line.split()[1]) for line in union)
            duplicates = sorted(
                {value for value in covered if covered.count(value) > 1}
            )
            # Ticks between the last checkpoint and the kill are re-executed on
            # recovery: that window is the documented cost of crash recovery.
            # It must not extend past the recovery point, so every duplicate has
            # to sit at or after the first tick the recovered process emitted.
            self.assertTrue(
                all(value >= first_after for value in duplicates),
                f"work committed before generation {selected_generation} was "
                f"replayed: {duplicates[:10]}",
            )
            self.assertEqual(covered[0], 0)
            # Contiguous coverage from 0 up to wherever the second run reached:
            # no tick was lost across the crash.
            distinct = sorted(set(covered))
            self.assertEqual(
                distinct, list(range(distinct[0], distinct[-1] + 1)),
                "recovery left a gap in the executed work",
            )

    def test_recovery_reports_the_selection_and_survives_a_corrupt_newest_slot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "ticker.py"
            program.write_text(PROGRAM, encoding="utf-8")
            checkpoints = root / "checkpoints"

            with open(root / "b.out", "wb") as out, open(root / "b.err", "wb") as err:
                source = subprocess.Popen(
                    continuum(
                        "run",
                        "--checkpoint-dir", str(checkpoints),
                        "--checkpoint-interval", "50ms",
                        str(program),
                    ),
                    cwd=REPOSITORY,
                    env=child_environment(),
                    stdout=out,
                    stderr=err,
                )
                try:
                    wait_for_generation(checkpoints, 4, source)
                    kill_hard(source)
                finally:
                    if source.poll() is None:  # pragma: no cover - cleanup only
                        source.kill()
                        source.wait(timeout=30)
            assert_dead(source)

            report = json.loads(
                subprocess.run(
                    continuum("checkpoints", str(checkpoints), "--json"),
                    cwd=REPOSITORY, env=child_environment(),
                    capture_output=True, text=True, check=True,
                ).stdout
            )
            newest = max(
                (item for item in report["slots"] if item["valid"]),
                key=lambda item: item["generation"],
            )
            older = min(
                (item for item in report["slots"] if item["valid"]),
                key=lambda item: item["generation"],
            )
            self.assertNotEqual(newest["slot"], older["slot"])
            # Destroy the newest committed slot, as a torn write would.
            (checkpoints / newest["slot"]).write_bytes(b"corrupted beyond repair")

            recovered = subprocess.run(
                continuum("recover", str(checkpoints), "--dry-run"),
                cwd=REPOSITORY, env=child_environment(),
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                f"Recovering generation {older['generation']}", recovered.stderr
            )
            # The refusal is reported, not silently swallowed.
            self.assertIn("refused", recovered.stderr)

    def test_recovery_refuses_a_directory_with_no_valid_checkpoint(self):
        with TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            completed = subprocess.run(
                continuum("recover", str(empty)),
                cwd=REPOSITORY, env=child_environment(),
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("no valid checkpoint", completed.stderr)


if __name__ == "__main__":
    unittest.main()
