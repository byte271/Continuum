from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from validation.cross_platform.common import (
    all_evidence_files,
    expected_cpython_source_sha256,
    verify_file_manifest,
    write_file_manifest,
)
from validation.cross_platform.qualification import (
    qualify_linux_source,
    qualify_macos_target,
)


REPOSITORY = Path(__file__).resolve().parents[1]


def github_environment(
    *,
    job: str,
    os_name: str,
    architecture: str,
    image_os: str,
) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": job,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "owner/continuum",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW": "Cross-platform continuation proof",
        "GITHUB_WORKFLOW_REF": (
            "owner/continuum/.github/workflows/"
            "cross-platform-proof.yml@refs/heads/main"
        ),
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "ImageOS": image_os,
        "ImageVersion": "20260727.1",
        "RUNNER_ARCH": architecture,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_NAME": "GitHub Actions 1",
        "RUNNER_OS": os_name,
    }


class CrossPlatformQualificationTests(unittest.TestCase):
    def linux_qualification(
        self,
        *,
        container_markers: tuple[str, ...] = (),
        emulation_markers: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
    ) -> tuple[bool, list[str], dict[str, str]]:
        return qualify_linux_source(
            system="Linux",
            machine="x86_64",
            uname_machine="x86_64",
            python_version="3.12.13",
            python_system="Linux",
            python_machine="x86_64",
            python_build_verified=True,
            container_markers=container_markers,
            emulation_markers=emulation_markers,
            git_commit="a" * 40,
            environment=environment
            or github_environment(
                job="linux-source",
                os_name="Linux",
                architecture="X64",
                image_os="ubuntu24",
            ),
        )

    def test_native_github_linux_x64_is_qualified(self):
        qualified, failures, metadata = self.linux_qualification()
        self.assertTrue(qualified)
        self.assertEqual(failures, [])
        self.assertEqual(metadata["RUNNER_ARCH"], "X64")
        self.assertEqual(
            metadata["WORKFLOW_RUN_URL"],
            "https://github.com/owner/continuum/actions/runs/123456789",
        )

    def test_application_container_is_rejected(self):
        qualified, failures, _ = self.linux_qualification(
            container_markers=("/.dockerenv",)
        )
        self.assertFalse(qualified)
        self.assertIn(
            "application-container markers were detected",
            failures,
        )

    def test_known_cpu_emulation_is_rejected(self):
        qualified, failures, _ = self.linux_qualification(
            emulation_markers=("qemu",)
        )
        self.assertFalse(qualified)
        self.assertIn("known CPU-emulation markers were detected", failures)

    def test_wrong_github_runner_architecture_is_rejected(self):
        environment = github_environment(
            job="linux-source",
            os_name="Linux",
            architecture="ARM64",
            image_os="ubuntu24",
        )
        qualified, failures, _ = self.linux_qualification(
            environment=environment
        )
        self.assertFalse(qualified)
        self.assertIn("RUNNER_ARCH must be 'X64'", failures)

    def test_native_github_apple_silicon_target_is_qualified(self):
        qualified, failures, metadata = qualify_macos_target(
            system="Darwin",
            machine="arm64",
            uname_machine="arm64",
            arch="arm64",
            cpu_brand="Apple M4",
            rosetta_translated=False,
            python_version="3.12.13",
            python_system="Darwin",
            python_machine="arm64",
            python_build_verified=True,
            git_commit="a" * 40,
            environment=github_environment(
                job="macos-target",
                os_name="macOS",
                architecture="ARM64",
                image_os="macos26",
            ),
        )
        self.assertTrue(qualified)
        self.assertEqual(failures, [])
        self.assertEqual(metadata["RUNNER_ARCH"], "ARM64")

    def test_rosetta_target_is_rejected(self):
        qualified, failures, _ = qualify_macos_target(
            system="Darwin",
            machine="arm64",
            uname_machine="arm64",
            arch="arm64",
            cpu_brand="Apple M4",
            rosetta_translated=True,
            python_version="3.12.13",
            python_system="Darwin",
            python_machine="arm64",
            python_build_verified=True,
            git_commit="a" * 40,
            environment=github_environment(
                job="macos-target",
                os_name="macOS",
                architecture="ARM64",
                image_os="macos26",
            ),
        )
        self.assertFalse(qualified)
        self.assertIn(
            "the target Python is running through Rosetta",
            failures,
        )

    def test_official_cpython_source_hash_is_pinned(self):
        self.assertEqual(
            expected_cpython_source_sha256(REPOSITORY),
            "c08bc65a81971c1dd578318282650336"
            "9466c7e67374d1646519adf05207b684",
        )

    def test_recursive_evidence_manifest_detects_nested_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            nested = directory / "runtime" / "sessions"
            nested.mkdir(parents=True)
            record = nested / "session.json"
            record.write_text('{"state":"frozen"}\n', encoding="utf-8")
            names = all_evidence_files(
                directory,
                exclude=("evidence.sha256",),
            )
            digest = write_file_manifest(
                directory,
                names,
                "evidence.sha256",
            )
            self.assertEqual(
                verify_file_manifest(directory, "evidence.sha256"),
                digest,
            )
            record.write_text('{"state":"changed"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                verify_file_manifest(directory, "evidence.sha256")

    def test_evidence_manifest_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaisesRegex(RuntimeError, "unsafe evidence filename"):
                write_file_manifest(
                    directory,
                    ("../outside.txt",),
                    "evidence.sha256",
                )


if __name__ == "__main__":
    unittest.main()
