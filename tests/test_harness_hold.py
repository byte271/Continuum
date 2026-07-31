"""Regression for the source-side freeze orchestration.

`validation/cross_platform/source_linux.py` generates release evidence and
only runs inside the Linux proof job, so a defect there is discovered by a
failed release rather than by CI. Its freeze ordering now lives in
`continuum._harness.freeze_held_source`, which this module exercises directly
and repeatedly on any host.

The property under test is the one that failed in v0.3.0: the source must
still be alive, holding at a safe point, when the freeze request appears.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from continuum._harness import (
    DEFAULT_HOLD_SAFE_POINT,
    environment_for,
    freeze_held_source,
    wait_for_ready,
)

ROOT = Path(__file__).resolve().parents[1]

# Short enough to repeat, long enough that an unheld source would finish
# first on a fast host. That is precisely the condition that broke v0.3.0.
WORKLOAD = (
    "total = 0\n"
    "index = 0\n"
    "while index < 4000:\n"
    "    total = total + index\n"
    "    index = index + 1\n"
    "print('FINAL', total)\n"
)


class SourceSideFreezeOrchestrationTests(unittest.TestCase):
    def _run_once(self, temporary: Path) -> dict:
        program = temporary / "workload.py"
        program.write_text(WORKLOAD, encoding="utf-8")
        sync_dir = temporary / "sync"
        sync_dir.mkdir()
        image = temporary / "held.cont"

        environment = {**os.environ, "CONTINUUM_HOME": str(temporary / "home")}
        environment["PYTHONPATH"] = str(ROOT)
        source_environment = environment_for(
            sync_dir, environment, hold_safe_point=200
        )

        source = subprocess.Popen(
            [sys.executable, "-m", "continuum", "run", str(program)],
            cwd=str(ROOT),
            env=source_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            session_line = source.stderr.readline().strip()
            self.assertTrue(
                session_line.startswith("Continuum session: "), session_line
            )
            session_id = session_line.split(": ", 1)[1]

            ready = wait_for_ready(source, sync_dir)
            evidence = freeze_held_source(
                [sys.executable, "-m", "continuum"],
                session_id,
                image,
                source,
                sync_dir,
                ready,
                cwd=ROOT,
                env=environment,
            )
            source.wait(timeout=60)
            return evidence
        finally:
            if source.poll() is None:
                source.kill()
            source.communicate(timeout=30)

    def test_orchestration_holds_the_source_every_time(self):
        # Repeated deliberately: the defect this guards was intermittent, so a
        # single pass proves nothing. Kept modest here; the stress workflow
        # repeats this target many more times.
        for repetition in range(5):
            with self.subTest(repetition=repetition):
                with tempfile.TemporaryDirectory() as temporary:
                    evidence = self._run_once(Path(temporary))

                self.assertEqual(evidence["returncode"], 0, evidence["stderr"])
                # The ordering guarantees, as observed rather than assumed.
                self.assertTrue(evidence["source_alive_when_request_published"])
                self.assertTrue(evidence["request_published_before_release"])
                self.assertTrue(
                    evidence["readiness_published_before_freeze_client"]
                )
                self.assertEqual(
                    evidence["synchronization"],
                    "safe-point hold, not an output marker",
                )
                # Real work happened before the checkpoint.
                self.assertGreaterEqual(evidence["safe_points_at_hold"], 200)
                self.assertGreater(evidence["instructions_at_hold"], 200)

    def test_hold_position_is_identical_across_runs(self):
        # The hold is an execution position, so a fast and a slow run must
        # checkpoint in the same place. A timing-based scheme would not.
        positions = []
        for _ in range(3):
            with tempfile.TemporaryDirectory() as temporary:
                positions.append(
                    self._run_once(Path(temporary))["safe_points_at_hold"]
                )
        self.assertEqual(len(set(positions)), 1, positions)

    def test_image_is_committed_and_source_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._run_once(root)
            self.assertEqual(evidence["returncode"], 0)
            image = root / "held.cont"
            self.assertTrue(image.exists(), "no image was committed")
            self.assertGreater(image.stat().st_size, 0)

    def test_proof_generator_uses_the_shared_orchestration(self):
        # Guards against the proof generator drifting back to an output
        # marker, which is how it was written before v0.3.1.
        source = (ROOT / "validation" / "cross_platform" / "source_linux.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("harness_freeze_held_source", source)
        self.assertIn("harness_wait_for_ready", source)
        self.assertNotIn("subprocess.run(\n            [\n                sys.executable,\n                \"-m\",\n                \"continuum\",\n                \"freeze\"", source)

    def test_default_hold_is_past_real_work_for_the_demo_workload(self):
        self.assertGreater(DEFAULT_HOLD_SAFE_POINT, 15_025)

    def test_proof_hold_is_past_the_action_the_proof_requires(self):
        """The hold must not stop the source before its required work.

        The proof waits for a thirtieth recorded action. A hold placed before
        that point stops the source, the action is never printed, and the run
        deadlocks: that is exactly how run 30604486120 failed. ITER 30 is at
        safe point 22,737 and ITER 40 at 25,308 for the proof workload.
        """
        source = (
            ROOT / "validation" / "cross_platform" / "source_linux.py"
        ).read_text(encoding="utf-8")
        match = re.search(r"PROOF_HOLD_SAFE_POINT = ([0-9_]+)", source)
        self.assertIsNotNone(match, "proof hold constant is missing")
        hold = int(match.group(1).replace("_", ""))
        self.assertGreater(hold, 22_737, "hold precedes the thirtieth action")
        self.assertLess(hold, 25_308, "hold is later than necessary")
        # And the deadlocking wait must not come back.
        self.assertNotIn("wait_for(stdout_log", source)


if __name__ == "__main__":
    unittest.main()
