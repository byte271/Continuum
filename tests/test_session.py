from __future__ import annotations

import json
import hashlib
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from continuum.compiler import compile_source
from continuum.errors import ContinuumError, FrozenExecution
from continuum.session import (
    read_published_json,
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
            if controller._signal_notifications:
                self._wait_until(lambda: controller.freeze_requested)
            else:
                controller._next_request_poll = 0.0
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
            if controller._signal_notifications:
                self._wait_until(lambda: controller.freeze_requested)
            else:
                controller._next_request_poll = 0.0
            with self.assertRaises(FrozenExecution):
                controller.on_safe_point(vm)
            second.join(timeout=5)
            self.assertFalse(second.is_alive())
            self.assertIsInstance(outcomes[1], dict)
            self.assertTrue(second_image.exists())
            controller.finish("frozen")

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGUSR1"),
        "signal-notification behavior requires POSIX SIGUSR1",
    )
    def test_idle_signal_safe_point_does_not_touch_the_filesystem(self):
        source = "value = 1\n"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CONTINUUM_HOME": str(Path(temporary) / "home")},
        ):
            controller = SessionController(source, "idle.py")
            controller.start()
            try:
                with patch.object(
                    Path,
                    "exists",
                    side_effect=AssertionError("filesystem polled"),
                ):
                    controller.on_safe_point(object())
            finally:
                controller.finish("completed")

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGUSR1"),
        "signal notification requires POSIX SIGUSR1",
    )
    def test_freeze_request_notifies_source_after_atomic_publication(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CONTINUUM_HOME": str(Path(temporary) / "home")},
        ):
            controller = SessionController("pass\n", "notify.py")
            controller.start()
            observed = {}

            def fake_kill(pid, signum):
                observed["pid"] = pid
                observed["signal"] = signum
                observed["request_exists"] = controller.request_path.exists()
                raise ProcessLookupError("test stop")

            try:
                with patch("continuum.session.os.kill", side_effect=fake_kill):
                    with self.assertRaisesRegex(
                        ContinuumError, "cannot notify session process"
                    ):
                        request_freeze(
                            controller.session_id,
                            str(Path(temporary) / "state.cont"),
                        )
            finally:
                controller.finish("completed")

            self.assertEqual(observed["pid"], os.getpid())
            self.assertEqual(observed["signal"], signal.SIGUSR1)
            self.assertTrue(observed["request_exists"])
            self.assertFalse(controller.request_path.exists())

    def test_polling_notification_freezes_at_a_safe_point(self):
        source = """
index = 0
while index < 10:
    index += 1
"""
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"CONTINUUM_HOME": str(Path(temporary) / "home")},
            ),
            patch("continuum.session._uses_signal_notifications", return_value=False),
        ):
            controller = SessionController(source, "polling.py")
            controller.start()
            vm = VirtualMachine(
                compile_source(source, "polling.py"),
                ["polling.py"],
                "polling.py",
            )
            image = Path(temporary) / "polling.cont"
            outcomes: list[BaseException | dict] = []

            def request() -> None:
                try:
                    outcomes.append(
                        request_freeze(controller.session_id, str(image))
                    )
                except BaseException as exc:
                    outcomes.append(exc)

            worker = threading.Thread(target=request)
            worker.start()
            self._wait_for(controller.request_path)
            controller._next_request_poll = 0.0
            with self.assertRaises(FrozenExecution):
                controller.on_safe_point(vm)
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], dict)
            self.assertTrue(image.exists())
            controller.finish("frozen")

    @staticmethod
    def _wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting for {path}")

    @staticmethod
    def _wait_until(predicate) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("timed out waiting for condition")


class PublishedDocumentReadTests(unittest.TestCase):
    """The shared reader for atomically published control documents.

    v0.3.0 failed intermittently on Windows because a reader opened a document
    during the atomic replace that published it and treated the resulting
    EACCES as fatal. Every published document is now read through one helper.
    """

    def test_reads_a_published_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "doc.json"
            path.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(read_published_json(path), {"ok": True})

    def test_retries_while_the_publication_is_in_flight(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "doc.json"
            path.write_text('{"ok": true}', encoding="utf-8")
            attempts = []
            real = Path.read_text

            def flaky(self, *args, **kwargs):
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError(13, "in flight")
                return real(self, *args, **kwargs)

            with mock.patch.object(Path, "read_text", flaky):
                self.assertEqual(read_published_json(path), {"ok": True})
            self.assertEqual(len(attempts), 3)

    def test_gives_up_after_the_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "doc.json"
            path.write_text("{}", encoding="utf-8")

            def always_locked(self, *args, **kwargs):
                raise PermissionError(13, "locked")

            with mock.patch.object(Path, "read_text", always_locked):
                with self.assertRaises(PermissionError):
                    read_published_json(path, timeout=0.05)

    def test_malformed_document_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "doc.json"
            path.write_text("{not json", encoding="utf-8")
            # Atomic publication means a readable document is never partial,
            # so this is corruption and must fail immediately.
            with self.assertRaises(json.JSONDecodeError):
                read_published_json(path, timeout=5.0)

    def test_missing_document_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                read_published_json(Path(temporary) / "absent.json", timeout=5.0)


if __name__ == "__main__":
    unittest.main()
