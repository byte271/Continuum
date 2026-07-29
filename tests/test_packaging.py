import hashlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_runtime_bundle_workflow_has_both_native_jobs(self):
        workflow = (
            ROOT / ".github" / "workflows" / "runtime-bundles.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("linux-x86_64:", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("macos-arm64:", workflow)
        self.assertIn("runs-on: macos-26", workflow)
        self.assertIn("build_bundle.sh linux-x86_64", workflow)
        self.assertIn("build_bundle.sh macos-arm64", workflow)
        self.assertEqual(workflow.count("-m unittest discover -s tests -v"), 2)
        self.assertEqual(workflow.count("packaging/install.sh"), 2)

    def test_archive_builder_is_deterministic_and_normalizes_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_bundle = root / "first" / "continuum-linux-x86_64"
            second_bundle = root / "second" / "continuum-linux-x86_64"
            for bundle in (first_bundle, second_bundle):
                (bundle / "bin").mkdir(parents=True)
                (bundle / "bin" / "continuum").write_text(
                    "#!/bin/sh\nexit 0\n", encoding="utf-8"
                )
                (bundle / "runtime-manifest.json").write_text(
                    '{"self_contained":true}\n', encoding="utf-8"
                )
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            command = ROOT / "packaging" / "archive_bundle.py"
            subprocess.run(
                [sys.executable, str(command), str(first_bundle), str(first)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [sys.executable, str(command), str(second_bundle), str(second)],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
            self.assertEqual(
                first.with_name(f"{first.name}.sha256")
                .read_text(encoding="ascii")
                .split()[0],
                digest,
            )
            with tarfile.open(first, "r:gz") as archive:
                for member in archive.getmembers():
                    self.assertEqual(member.uid, 0)
                    self.assertEqual(member.gid, 0)
                    self.assertEqual(member.mtime, 0)
                    self.assertTrue(
                        member.name == "continuum-linux-x86_64"
                        or member.name.startswith("continuum-linux-x86_64/")
                    )

    def test_installer_rejects_invalid_digest_before_download(self):
        result = subprocess.run(
            [
                "sh",
                str(ROOT / "packaging" / "install.sh"),
                "--archive",
                "https://example.invalid/continuum.tar.gz",
                "--sha256",
                "not-a-digest",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly 64 lowercase", result.stderr)


if __name__ == "__main__":
    unittest.main()
