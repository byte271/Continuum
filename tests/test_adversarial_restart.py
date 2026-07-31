from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import json
import uuid
from pathlib import Path

from continuum.cli import (
    DEMO_HOLD_SAFE_POINT,
    DEMO_HOLD_SAFE_POINT_ENV,
    DEMO_SYNC_ENV,
)


AUDITOR = r"""
import os
import sys

with open(sys.argv[1], "a", encoding="utf-8", buffering=1) as output:
    for line in sys.stdin:
        output.write(line)
        output.flush()
        os.fsync(output.fileno())
"""


PROGRAM = """
import hashlib

print("ACTION {nonce} ENTRY", flush=True)

def level_one(limit):
    print("ACTION {nonce} PROLOGUE_ONE", flush=True)
    return level_two(limit)

def level_two(limit):
    print("ACTION {nonce} PROLOGUE_TWO", flush=True)
    return worker(limit)

def worker(limit):
    print("ACTION {nonce} PROLOGUE_WORKER", flush=True)
    index = 0
    accumulator = 2166136261
    while index < limit:
        if index < 40:
            print(f"ACTION {nonce} ITER_{{index}}", flush=True)
        accumulator = (accumulator * 16777619 + index) % 4294967296
        index += 1
    return hashlib.sha256(str(accumulator).encode()).hexdigest()

final_hash = level_one(int(__args__[1]))
print("FINAL_HASH", final_hash, flush=True)
"""


def _wait_for_text(path: Path, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if needle in path.read_text(encoding="utf-8"):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {needle!r} in external audit log")


class AdversarialRestartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.repository = Path(__file__).resolve().parents[1]
        cls.nonce = uuid.uuid4().hex
        cls.program = cls.root / "irreversible.py"
        cls.program.write_text(
            PROGRAM.format(nonce=cls.nonce),
            encoding="utf-8",
        )
        cls.audit_log = cls.root / "irreversible-actions.log"
        cls.image = cls.root / "state.cont"
        environment = os.environ.copy()
        environment["CONTINUUM_HOME"] = str(cls.root / "home")
        environment["PYTHONPATH"] = str(cls.repository)
        # Hold the source at a safe point until the freeze request is on disk.
        # Observing audit output and then racing `continuum freeze` let a fast
        # host finish the workload first, which failed this proof outright in
        # roughly one run in eight. The hold is an execution position, so the
        # checkpoint lands in the same place on every host.
        cls.sync = cls.root / "sync"
        cls.sync.mkdir()
        source_environment = {
            **environment,
            DEMO_SYNC_ENV: str(cls.sync),
            DEMO_HOLD_SAFE_POINT_ENV: str(DEMO_HOLD_SAFE_POINT),
        }

        auditor = subprocess.Popen(
            [sys.executable, "-c", AUDITOR, str(cls.audit_log)],
            stdin=subprocess.PIPE,
            cwd=cls.repository,
            env=environment,
            text=True,
        )
        if auditor.stdin is None:
            raise AssertionError("auditor pipe was not created")
        source = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "continuum",
                "run",
                str(cls.program),
                "120000",
            ],
            stdout=auditor.stdin,
            stderr=subprocess.PIPE,
            cwd=cls.repository,
            env=source_environment,
            text=True,
        )
        if source.stderr is None:
            raise AssertionError("source stderr pipe was not created")
        session_line = source.stderr.readline().strip()
        if not session_line.startswith("Continuum session: "):
            raise AssertionError(f"missing session ID: {session_line}")
        session_id = session_line.split(": ", 1)[1]
        _wait_for_text(cls.audit_log, f"ACTION {cls.nonce} ITER_10")
        # Wait for the held source to publish its readiness document.
        deadline = time.monotonic() + 120
        ready_path = cls.sync / "ready.json"
        while time.monotonic() < deadline and not ready_path.exists():
            if source.poll() is not None:
                raise AssertionError("source exited before reaching its hold")
            time.sleep(0.005)
        if not ready_path.exists():
            raise AssertionError("source never reached its hold safe point")
        request_path = Path(
            json.loads(ready_path.read_text(encoding="utf-8"))["request_path"]
        )

        freeze = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "continuum",
                "freeze",
                session_id,
                "-o",
                str(cls.image),
            ],
            cwd=cls.repository,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Only release the source once the request it must observe exists.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not request_path.exists():
            if freeze.poll() is not None:
                break
            time.sleep(0.005)
        (cls.sync / "start").touch()
        freeze_stdout, freeze_stderr = freeze.communicate(timeout=120)
        if freeze.returncode != 0:
            source.kill()
            auditor.stdin.close()
            auditor.kill()
            raise AssertionError(f"freeze failed: {freeze_stderr or freeze_stdout}")
        cls.source_pid = source.pid
        source.wait(timeout=10)
        cls.source_returncode = source.returncode
        source.stderr.close()

        target = subprocess.Popen(
            [sys.executable, "-m", "continuum", "resume", str(cls.image)],
            stdout=auditor.stdin,
            stderr=subprocess.PIPE,
            cwd=cls.repository,
            env=environment,
            text=True,
        )
        cls.target_pid = target.pid
        _, cls.target_stderr = target.communicate(timeout=30)
        cls.target_returncode = target.returncode
        auditor.stdin.close()
        auditor.wait(timeout=10)
        cls.audit_lines = cls.audit_log.read_text(encoding="utf-8").splitlines()

        control = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum",
                "run",
                str(cls.program),
                "120000",
            ],
            cwd=cls.repository,
            env={
                **environment,
                "CONTINUUM_HOME": str(cls.root / "control-home"),
            },
            text=True,
            capture_output=True,
            timeout=30,
        )
        if control.returncode != 0:
            raise AssertionError(f"control run failed: {control.stderr}")
        cls.control_stdout = control.stdout

    def test_entry_module_and_source_process_do_not_restart(self):
        self.assertEqual(self.source_returncode, 0)
        self.assertEqual(self.target_returncode, 0, self.target_stderr)
        self.assertNotEqual(self.source_pid, self.target_pid)
        self.assertEqual(
            self.audit_lines.count(f"ACTION {self.nonce} ENTRY"),
            1,
        )

    def test_completed_function_prologues_do_not_repeat(self):
        for name in ("PROLOGUE_ONE", "PROLOGUE_TWO", "PROLOGUE_WORKER"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.audit_lines.count(f"ACTION {self.nonce} {name}"),
                    1,
                )

    def test_completed_loop_actions_do_not_repeat_and_result_matches_control(self):
        actions = [
            line
            for line in self.audit_lines
            if line.startswith(f"ACTION {self.nonce} ITER_")
        ]
        self.assertGreaterEqual(len(actions), 11)
        self.assertEqual(len(actions), len(set(actions)))
        migrated_hashes = [
            match.group(1)
            for line in self.audit_lines
            if (match := re.fullmatch(r"FINAL_HASH ([0-9a-f]{64})", line))
        ]
        control_hashes = re.findall(
            r"^FINAL_HASH ([0-9a-f]{64})$",
            self.control_stdout,
            re.MULTILINE,
        )
        self.assertEqual(len(migrated_hashes), 1)
        self.assertEqual(migrated_hashes, control_hashes)


if __name__ == "__main__":
    unittest.main()
