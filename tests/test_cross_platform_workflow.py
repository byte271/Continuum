from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "cross-platform-proof.yml"
)


class CrossPlatformWorkflowContractTests(unittest.TestCase):
    def test_two_native_jobs_and_evidence_transfer_are_fixed_in_workflow(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        required_fragments = (
            "workflow_dispatch:",
            "linux-source:",
            "runs-on: ubuntu-24.04",
            "macos-target:",
            "needs: linux-source",
            "runs-on: macos-26",
            "build_cpython.sh",
            "source_linux.py",
            "target_macos.py",
            "continuum-linux-evidence.tar.sha256",
            "verify_evidence.py",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
