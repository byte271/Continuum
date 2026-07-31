from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from continuum._harness import (
    environment_for as harness_environment,
    release as harness_release,
    wait_for_ready as harness_wait_for_ready,
    wait_for_request as harness_wait_for_request,
)


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
            sync_dir = Path(temporary) / "sync"
            sync_dir.mkdir()
            source_environment = harness_environment(sync_dir, environment)
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
                env=source_environment,
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
            # Hold at a real safe point instead of racing the workload: the
            # sentinel says work started, not that the source will still be
            # alive when the freeze request lands.
            ready = harness_wait_for_ready(source, sync_dir)
            request_path = Path(ready["request_path"])
            freeze = subprocess.Popen(
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            harness_wait_for_request(request_path, source, freeze)
            self.assertIsNone(source.poll(), "source died before release")
            harness_release(sync_dir)
            freeze_stdout, freeze_stderr = freeze.communicate(timeout=120)
            self.assertEqual(freeze.returncode, 0, freeze_stderr or freeze_stdout)
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
            source_architecture = {
                "amd64": "x86_64",
                "x64": "x86_64",
                "aarch64": "arm64",
            }.get(platform.machine().lower(), platform.machine().lower())
            self.assertIn(
                f"Restored from {platform.system()} {source_architecture}.",
                target_stderr,
            )


if __name__ == "__main__":
    unittest.main()
