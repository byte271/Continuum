import hashlib
import os
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

    def test_installed_launcher_resolves_bundle_through_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "lib" / "continuum-linux-x86_64"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "runtime" / "bin").mkdir(parents=True)
            (bundle / "app").mkdir()
            launcher = bundle / "bin" / "continuum"
            launcher.write_bytes((ROOT / "packaging" / "continuum").read_bytes())
            launcher.chmod(0o755)
            fake_python = bundle / "runtime" / "bin" / "python3.12"
            fake_python.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$PYTHONHOME" "$PYTHONPATH" '
                '"$CONTINUUM_BUNDLE_MANIFEST" "$*"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            (root / "bin").mkdir()
            command = root / "bin" / "continuum"
            command.symlink_to("../lib/continuum-linux-x86_64/bin/continuum")

            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [str(command), "doctor"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            lines = result.stdout.splitlines()
            self.assertEqual(Path(lines[0]).resolve(), (bundle / "runtime").resolve())
            self.assertEqual(Path(lines[1]).resolve(), (bundle / "app").resolve())
            self.assertEqual(
                Path(lines[2]).resolve(),
                (bundle / "runtime-manifest.json").resolve(),
            )
            self.assertEqual(lines[3], "-m continuum doctor")


if __name__ == "__main__":
    unittest.main()
