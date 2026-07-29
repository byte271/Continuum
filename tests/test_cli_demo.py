import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from continuum.cli import _demo_final_hash, _demo_marker_counts


class CliDemoTests(unittest.TestCase):
    def test_proof_markers_accept_windows_crlf_without_substring_matches(self):
        content = (
            b"IDENTITY True True\r\n"
            b"IDENTITY True True EXTRA\r\n"
            b"FINAL " + b"a" * 64 + b"\r\n"
        )
        self.assertEqual(_demo_marker_counts(content), (1, 1))
        self.assertEqual(_demo_final_hash(content), "a" * 64)

    def test_demo_freezes_resumes_and_matches_control(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence"
            environment = {
                **os.environ,
                "CONTINUUM_HOME": str(Path(temporary) / "outer-home"),
            }
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "continuum",
                    "demo",
                    "--output-dir",
                    str(evidence),
                    "--iterations",
                    "5000",
                ],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Same-machine continuation demonstration", result.stdout
            )
            self.assertIn(
                "Combined output matches uninterrupted control: yes",
                result.stdout,
            )
            comparison = json.loads(
                (evidence / "comparison.json").read_text(encoding="utf-8")
            )
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


if __name__ == "__main__":
    unittest.main()
