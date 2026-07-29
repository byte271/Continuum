import argparse
import io
import json
import os
import platform
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from continuum import SUPPORTED_PYTHON, __version__
from continuum.cli import _doctor


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_supported_source_checkout(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["continuum_version"], __version__)
        self.assertEqual(report["python_version"], SUPPORTED_PYTHON)
        self.assertFalse(report["self_contained"])
        self.assertEqual(report["problems"], [])

    def test_doctor_rejects_wrong_python_version(self):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(platform, "python_version", return_value="3.12.12"),
            redirect_stdout(output),
        ):
            result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertIn("exact CPython 3.12.13 is required", report["problems"][0])

    def test_doctor_validates_self_contained_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "runtime-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "continuum_version": __version__,
                        "python_version": SUPPORTED_PYTHON,
                        "system": platform.system(),
                        "architecture": platform.machine(),
                        "self_contained": True,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"CONTINUUM_BUNDLE_MANIFEST": str(manifest)},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["self_contained"])
        self.assertEqual(report["problems"], [])


if __name__ == "__main__":
    unittest.main()
