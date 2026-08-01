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
from continuum.abi import (
    CONTAINER_FORMAT_VERSION,
    VERIFIED_PYTHON_VERSIONS,
    build_contract,
)
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
        # Doctor must succeed on every verified interpreter, not only on the
        # one the exact-version path shipped against.
        self.assertIn(report["python_version"], VERIFIED_PYTHON_VERSIONS)
        self.assertEqual(report["required_python_version"], SUPPORTED_PYTHON)
        self.assertEqual(
            report["verified_python_versions"], list(VERIFIED_PYTHON_VERSIONS)
        )
        self.assertEqual(
            report["verified_cross_platform_paths"],
            ["Linux x86_64 -> macOS arm64"],
        )
        self.assertEqual(
            report["verified_same_host_targets"],
            ["Linux x86_64", "macOS arm64", "Windows x86_64"],
        )
        self.assertIn("Windows x86_64", report["format_compatible_targets"])
        self.assertIn(
            "runtime-bundles.yml",
            report["evidence"]["verified_same_host_targets"],
        )
        self.assertIn(
            "cross-platform-proof.yml",
            report["evidence"]["verified_cross_platform_paths"],
        )
        self.assertFalse(report["self_contained"])
        self.assertEqual(report["problems"], [])

    def test_doctor_never_reports_format_compatibility_as_verified(self):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CONTINUUM_BUNDLE_MANIFEST": ""}),
            redirect_stdout(output),
        ):
            _doctor(argparse.Namespace(json=True))
        report = json.loads(output.getvalue())

        # A pair the format accepts must never imply a proven path. Windows is
        # format-compatible and same-host verified, but no cross-platform
        # workflow has ever produced or resumed a Windows image.
        self.assertNotEqual(
            report["format_compatible_targets"],
            report["verified_same_host_targets"],
        )
        for path in report["verified_cross_platform_paths"]:
            self.assertNotIn("Windows", path)
        for target in report["verified_same_host_targets"]:
            self.assertIn(target, report["format_compatible_targets"])

        text = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CONTINUUM_BUNDLE_MANIFEST": ""}),
            redirect_stdout(text),
        ):
            _doctor(argparse.Namespace(json=False))
        rendered = text.getvalue()
        self.assertIn("not evidence of a verified continuation path", rendered)
        self.assertIn(
            "Verified cross-platform continuation: "
            "Linux x86_64 -> macOS arm64",
            rendered,
        )

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
        # 3.12.12 is one patch below a verified version: close is still refused,
        # because the allowlist is exact rather than a range.
        self.assertIn("is not verified by this runtime", report["problems"][0])
        self.assertIn("3.12.12", report["problems"][0])

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
        self.assertIn("Windows x86_64", report["format_compatible_targets"])
        self.assertEqual(report["current_target"], "Windows x86_64")
        self.assertTrue(report["current_target_same_host_verified"])
        self.assertNotIn(
            "Windows x86_64 -> ",
            " ".join(report["verified_cross_platform_paths"]),
        )

    def test_doctor_validates_self_contained_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "runtime-manifest.json"
            architecture = {
                "amd64": "x86_64",
                "x64": "x86_64",
                "aarch64": "arm64",
            }.get(platform.machine().lower(), platform.machine().lower())
            manifest.write_text(
                json.dumps(
                    {
                        "continuum_version": __version__,
                        "ir_version": IR_VERSION,
                        "python_version": SUPPORTED_PYTHON,
                        "system": platform.system(),
                        "architecture": architecture,
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
                "format_version": CONTAINER_FORMAT_VERSION,
                "execution_contract": build_contract(
                    "Windows", "x86_64", SUPPORTED_PYTHON, __version__
                ),
            }

            def validate_compatibility(self):
                observed["validated"] = True
                return build_contract(
                    "Windows", "x86_64", SUPPORTED_PYTHON, __version__
                )

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
