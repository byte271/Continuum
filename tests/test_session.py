from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from continuum.compiler import compile_source
from continuum.errors import ContinuumError, FrozenExecution
from continuum.session import (
    SessionController,
    _create_json_exclusive,
    request_freeze,
)
from continuum.vm import VirtualMachine


class SessionControlTests(unittest.TestCase):
    def test_request_is_not_visible_until_json_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = Path(temporary) / "request.json"
            started = threading.Event()
            release = threading.Event()
            real_dump = json.dump

            def slow_dump(value, handle, **kwargs):
                handle.write("{")
                handle.flush()
                started.set()
                release.wait(timeout=5)
                handle.seek(0)
                handle.truncate()
                return real_dump(value, handle, **kwargs)

            worker = threading.Thread(
                target=_create_json_exclusive,
                args=(request, {"command": "freeze", "value": 7}),
            )
            with patch("continuum.session.json.dump", side_effect=slow_dump):
                worker.start()
                self.assertTrue(started.wait(timeout=5))
                self.assertFalse(request.exists())
                release.set()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(
                json.loads(request.read_text(encoding="utf-8")),
                {"command": "freeze", "value": 7},
            )

    def test_failed_checkpoint_keeps_source_recoverable_and_allows_retry(self):
        source = """
index = 0
while index < 10:
    index += 1
"""
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CONTINUUM_HOME": str(Path(temporary) / "home")},
        ):
            controller = SessionController(source, "retry.py")
            controller.start()
            vm = VirtualMachine(
                compile_source(source, "retry.py"),
                ["retry.py"],
                "retry.py",
            )
            vm.globals["unsupported"] = hashlib.sha256()
            first_image = Path(temporary) / "first.cont"
            outcomes: list[BaseException | dict] = []

            def request(path: Path) -> None:
                try:
                    outcomes.append(request_freeze(controller.session_id, str(path)))
                except BaseException as exc:
                    outcomes.append(exc)

            first = threading.Thread(target=request, args=(first_image,))
            first.start()
            self._wait_for(controller.request_path)
            controller.on_safe_point(vm)
            first.join(timeout=5)
            self.assertFalse(first.is_alive())
            self.assertIsInstance(outcomes[0], ContinuumError)
            self.assertFalse(first_image.exists())
            self.assertTrue(vm.frames)
            self.assertFalse(controller.request_path.exists())
            self.assertFalse(controller.response_path.exists())

            del vm.globals["unsupported"]
            second_image = Path(temporary) / "second.cont"
            second = threading.Thread(target=request, args=(second_image,))
            second.start()
            self._wait_for(controller.request_path)
            with self.assertRaises(FrozenExecution):
                controller.on_safe_point(vm)
            second.join(timeout=5)
            self.assertFalse(second.is_alive())
            self.assertIsInstance(outcomes[1], dict)
            self.assertTrue(second_image.exists())

    @staticmethod
    def _wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting for {path}")


if __name__ == "__main__":
    unittest.main()
