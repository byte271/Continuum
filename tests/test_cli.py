import argparse
import io
import json
import os
import platform
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from continuum import IR_VERSION, SUPPORTED_PYTHON, __version__
from continuum.cli import _doctor, _resume


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_supported_source_checkout(self):
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"CONTINUUM_BUNDLE_MANIFEST": ""},
        ), redirect_stdout(output):
            result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["continuum_version"], __version__)
        self.assertEqual(report["continuum_ir_version"], IR_VERSION)
        self.assertEqual(report["python_version"], SUPPORTED_PYTHON)
        self.assertIn(
            "fresh exact-commit proof",
            report["current_runtime_cross_platform"],
        )
        self.assertIn("IR 0.3", report["verified_migration"])
        self.assertIn("30497170058", report["verified_migration"])
        self.assertFalse(report["self_contained"])
        self.assertEqual(report["problems"], [])

    def test_doctor_rejects_wrong_python_version(self):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CONTINUUM_BUNDLE_MANIFEST": ""}),
            mock.patch.object(platform, "python_version", return_value="3.12.12"),
            redirect_stdout(output),
        ):
            result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertIn("exact CPython 3.12.13 is required", report["problems"][0])

    def test_doctor_accepts_windows_x86_64(self):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CONTINUUM_BUNDLE_MANIFEST": ""}),
            mock.patch.object(platform, "system", return_value="Windows"),
            mock.patch.object(platform, "machine", return_value="AMD64"),
            redirect_stdout(output),
        ):
            result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["os"], "Windows")
        self.assertEqual(report["architecture"], "x86_64")
        self.assertIn("Windows x86_64", report["compatible_image_targets"])

    def test_doctor_validates_self_contained_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "runtime-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "continuum_version": __version__,
                        "ir_version": IR_VERSION,
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
                ),
                redirect_stdout(output),
            ):
                result = _doctor(argparse.Namespace(json=True))

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["self_contained"])
        self.assertEqual(report["problems"], [])

    def test_resume_preserves_foreign_absolute_relocation_key(self):
        observed = {}

        class FakeVm:
            def run(self):
                observed["ran"] = True

        class FakeImage:
            manifest = {
                "source": {"os": "Windows", "architecture": "x86_64"},
                "target_compatibility": {
                    "runtime_version": __version__,
                    "python_version": SUPPORTED_PYTHON,
                },
            }

            def validate_compatibility(self):
                observed["validated"] = True

            def restore_vm(self, policy, relocations):
                observed["policy"] = policy
                observed["relocations"] = relocations
                return FakeVm()

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target.txt"
            args = SimpleNamespace(
                image="state.cont",
                file_policy="relocate",
                relocate=[rf"C:\source\data.txt={target}"],
            )
            with mock.patch("continuum.cli.load_image", return_value=FakeImage()):
                result = _resume(args)

        self.assertEqual(result, 0)
        self.assertTrue(observed["validated"])
        self.assertTrue(observed["ran"])
        self.assertEqual(observed["policy"], "relocate")
        self.assertIn(r"C:\source\data.txt", observed["relocations"])


if __name__ == "__main__":
    unittest.main()
