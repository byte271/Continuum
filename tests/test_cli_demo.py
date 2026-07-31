import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from continuum import _harness
from continuum._harness import (
    DEFAULT_HOLD_SAFE_POINT,
    HOLD_SAFE_POINT_ENV,
    SYNC_ENV,
    HoldGate,
    safe_point_callback as harness_safe_point_callback,
)
from continuum.cli import _demo_final_hash, _demo_marker_counts
from continuum.errors import ContinuumError
from continuum.session import read_published_json

REPOSITORY = Path(__file__).resolve().parents[1]
# How many complete demonstrations the repetition regression runs. The race
# this guards was intermittent, so a single pass proves little.
DEMO_REPETITIONS = 3


def _demo_environment(home: Path) -> dict[str, str]:
    environment = {**os.environ, "CONTINUUM_HOME": str(home)}
    environment.pop("PYTHONPATH", None)
    for name in (SYNC_ENV, HOLD_SAFE_POINT_ENV):
        environment.pop(name, None)
    return environment


class CliDemoTests(unittest.TestCase):
    def test_proof_markers_accept_windows_crlf_without_substring_matches(self):
        content = (
            b"IDENTITY True True\r\n"
            b"IDENTITY True True EXTRA\r\n"
            b"FINAL " + b"a" * 64 + b"\r\n"
        )
        self.assertEqual(_demo_marker_counts(content), (1, 1))
        self.assertEqual(_demo_final_hash(content), "a" * 64)

    def _run_demo(self, temporary, iterations, name="evidence"):
        evidence = Path(temporary) / name
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum",
                "demo",
                "--output-dir",
                str(evidence),
                "--iterations",
                str(iterations),
            ],
            cwd=REPOSITORY,
            env=_demo_environment(Path(temporary) / f"{name}-outer-home"),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        comparison = json.loads(
            (evidence / "comparison.json").read_text(encoding="utf-8")
        )
        return result, evidence, comparison

    def _assert_continuation_evidence(self, comparison):
        self.assertTrue(comparison["new_target_process"])
        self.assertTrue(comparison["source_exited_before_target"])
        self.assertTrue(comparison["original_input_absent"])
        self.assertTrue(comparison["combined_output_matches_control"])
        self.assertTrue(comparison["final_hash_matches"])
        self.assertTrue(comparison["identity_proof_once"])
        self.assertTrue(comparison["final_output_once"])
        self.assertNotEqual(
            comparison["source_progress_last"],
            comparison["target_progress_first"],
        )

    def _assert_synchronized_freeze(self, comparison):
        # The freeze request existed on disk while the source was still held,
        # so no host can win a race against the freeze client.
        self.assertTrue(comparison["freeze_request_published_before_release"])
        self.assertTrue(comparison["source_alive_when_request_published"])
        self.assertTrue(comparison["source_made_progress_before_freeze"])
        self.assertEqual(comparison["hold_safe_point"], DEFAULT_HOLD_SAFE_POINT)
        self.assertGreaterEqual(
            comparison["source_safe_points_at_hold"], DEFAULT_HOLD_SAFE_POINT
        )
        self.assertIsNotNone(comparison["source_progress_last"])
        self.assertIsNotNone(comparison["target_progress_first"])

    def test_demo_freezes_resumes_and_matches_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, _, comparison = self._run_demo(temporary, 5_000)

            self.assertIn(
                "Same-machine continuation demonstration", result.stdout
            )
            self.assertIn(
                "Combined output matches uninterrupted control: yes",
                result.stdout,
            )
            self.assertIn(
                "Freeze request published while the source was held: yes",
                result.stdout,
            )
            self._assert_continuation_evidence(comparison)
            self._assert_synchronized_freeze(comparison)

    def test_repeated_demos_never_lose_the_freeze_race(self):
        """A fast host must not be able to finish before the freeze request.

        The original harness observed progress output and then raced the
        freeze client, which failed intermittently on fast Windows hosts.
        Repetition is the point of this test: the failure mode was
        nondeterministic, so one passing run proved nothing.
        """

        holds = []
        for repetition in range(DEMO_REPETITIONS):
            with self.subTest(repetition=repetition):
                with tempfile.TemporaryDirectory() as temporary:
                    _, _, comparison = self._run_demo(
                        temporary, 1_000, name=f"evidence-{repetition}"
                    )
                    self._assert_continuation_evidence(comparison)
                    self._assert_synchronized_freeze(comparison)
                    holds.append(comparison["source_safe_points_at_hold"])

        # The hold is an execution position, not a wall-clock guess, so every
        # repetition must stop the source at exactly the same place.
        self.assertEqual(len(set(holds)), 1, holds)

    def test_held_source_cannot_complete_before_release(self):
        """Prove the hold, not the timing.

        A full ungated workload is timed first, then an identical gated
        workload must still be alive and incomplete well past that duration,
        and must only finish once the start file is created.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = REPOSITORY / "examples" / "demo.py"
            source_input = REPOSITORY / "examples" / "demo_input.txt"
            command = [
                sys.executable,
                "-m",
                "continuum",
                "run",
                "--file-policy",
                "bundle",
                str(program),
                str(source_input),
                "1000",
            ]

            started = time.monotonic()
            ungated = subprocess.run(
                command,
                cwd=REPOSITORY,
                env=_demo_environment(root / "ungated-home"),
                capture_output=True,
                text=True,
                timeout=180,
            )
            ungated_seconds = time.monotonic() - started
            self.assertEqual(ungated.returncode, 0, ungated.stderr)
            self.assertIn("FINAL ", ungated.stdout)

            sync_dir = root / "sync"
            sync_dir.mkdir()
            ready_path = sync_dir / "ready.json"
            gated_stdout = root / "gated-stdout.log"
            environment = {
                **_demo_environment(root / "gated-home"),
                SYNC_ENV: str(sync_dir),
                HOLD_SAFE_POINT_ENV: str(DEFAULT_HOLD_SAFE_POINT),
            }

            with gated_stdout.open("w", encoding="utf-8") as handle:
                gated = subprocess.Popen(
                    command,
                    cwd=REPOSITORY,
                    env=environment,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        if ready_path.exists():
                            break
                        self.assertIsNone(
                            gated.poll(), "gated source exited before readiness"
                        )
                        time.sleep(0.005)
                    self.assertTrue(ready_path.exists(), "no readiness document")

                    ready = read_published_json(ready_path)
                    self.assertEqual(ready["pid"], gated.pid)
                    self.assertGreaterEqual(
                        ready["safe_points_executed"], DEFAULT_HOLD_SAFE_POINT
                    )

                    # Well past the time a complete ungated workload needs.
                    time.sleep(max(3 * ungated_seconds, 1.0))
                    self.assertIsNone(
                        gated.poll(),
                        "held source completed the workload without release",
                    )
                    held_output = gated_stdout.read_text(encoding="utf-8")
                    self.assertIn("Processing ", held_output)
                    self.assertNotIn("FINAL ", held_output)

                    (sync_dir / "start").touch()
                    self.assertEqual(gated.wait(timeout=120), 0)
                finally:
                    if gated.poll() is None:
                        gated.kill()
                    gated.communicate(timeout=60)

            self.assertIn(
                "FINAL ", gated_stdout.read_text(encoding="utf-8")
            )

    def test_start_gate_is_not_installed_outside_the_demo(self):
        controller = SimpleNamespace(
            on_safe_point=lambda vm: None,
            session_id="cont-000000000000",
            request_path=Path("unused"),
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SYNC_ENV, None)
            callback = harness_safe_point_callback(controller)
        self.assertIs(callback, controller.on_safe_point)

    def test_start_gate_rejects_an_invalid_hold_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller = SimpleNamespace(
                on_safe_point=lambda vm: None,
                session_id="cont-000000000000",
                request_path=Path("unused"),
            )
            with mock.patch.dict(
                os.environ,
                {
                    SYNC_ENV: temporary,
                    HOLD_SAFE_POINT_ENV: "not-a-number",
                },
            ):
                with self.assertRaises(ContinuumError) as caught:
                    harness_safe_point_callback(controller)
        self.assertIn(HOLD_SAFE_POINT_ENV, str(caught.exception))

    def test_start_gate_times_out_with_a_useful_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            sync_dir = Path(temporary)
            controller = SimpleNamespace(
                on_safe_point=lambda vm: None,
                session_id="cont-000000000000",
                request_path=sync_dir / "request.json",
            )
            gate = HoldGate(sync_dir, 1, controller)
            vm = SimpleNamespace(safe_points_executed=1, instructions_executed=42)
            with mock.patch.object(_harness, "START_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(ContinuumError) as caught:
                    gate(vm)

            message = str(caught.exception)
            self.assertIn("harness hold", message)
            self.assertIn(str(sync_dir / "start"), message)
            # Readiness is still published, so a controller can diagnose it.
            ready = json.loads(
                (sync_dir / "ready.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ready["session_id"], "cont-000000000000")
            self.assertEqual(ready["safe_points_executed"], 1)


if __name__ == "__main__":
    unittest.main()
