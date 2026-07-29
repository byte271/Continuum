from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProcessIndependentTests(unittest.TestCase):
    def test_source_exits_and_new_process_resumes_without_restart(self):
        repository = Path(__file__).resolve().parents[1]
        program = repository / "examples" / "anti_restart.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "process.cont"
            environment = os.environ.copy()
            environment["CONTINUUM_HOME"] = str(root / "home")
            environment["PYTHONPATH"] = str(repository)
            source = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "continuum",
                    "run",
                    str(program),
                    "300000",
                ],
                cwd=repository,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.addCleanup(
                lambda: source.poll() is None and source.kill()
            )
            session_line = source.stderr.readline().strip()
            self.assertTrue(session_line.startswith("Continuum session: "))
            session_id = session_line.split(": ", 1)[1]
            self.assertEqual(source.stdout.readline().strip(), "START_SENTINEL")
            freeze = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "continuum",
                    "freeze",
                    session_id,
                    "-o",
                    str(image),
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            source_pid = source.pid
            source.wait(timeout=10)
            self.assertEqual(source.returncode, 0)
            self.assertNotIn("FINAL_COUNT", source.stdout.read())
            source.stdout.close()
            source.stderr.close()

            target = subprocess.Popen(
                [sys.executable, "-m", "continuum", "resume", str(image)],
                cwd=repository,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            target_pid = target.pid
            target_stdout, target_stderr = target.communicate(timeout=30)
            self.assertEqual(target.returncode, 0, target_stderr)
            self.assertNotEqual(source_pid, target_pid)
            self.assertNotIn("START_SENTINEL", target_stdout)
            self.assertEqual(target_stdout.count("FINAL_COUNT 300000"), 1)
            self.assertIn("Restored from Linux x86_64.", target_stderr)


if __name__ == "__main__":
    unittest.main()
