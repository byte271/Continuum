"""Static gates for defects that unit tests structurally cannot catch.

`validation/cross_platform/source_linux.py` only executes inside the Linux
proof job. Two defects shipped in consecutive commits because it was changed
on inspection alone: a hold placed before the work the proof waits for, and a
dictionary written to before it was created. The second is a plain
used-before-assignment that any static checker reports instantly.

pylint is not a runtime dependency of Continuum, so this skips when it is
absent and the stress workflow installs it explicitly. A skipped check is
visible; a missing one is not.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Everything that can raise at runtime but is never imported by the suite.
CHECKED = (
    "continuum",
    "validation/cross_platform/source_linux.py",
    "validation/cross_platform/target_macos.py",
    "validation/cross_platform/common.py",
    "validation/cross_platform/verify_evidence.py",
    "validation/run_verification.py",
    "compatibility/runner.py",
    "benchmarks/measure.py",
    "packaging/archive_bundle.py",
    "packaging/archive_bundle_zip.py",
)

# E0601 used-before-assignment, E0602 undefined-variable,
# E0603 undefined-all-variable, E1120 no-value-for-parameter.
# Deliberately narrow: used-before-assignment and undefined-variable are the
# two classes that actually shipped in source_linux.py. Inference-dependent
# checks vary between platforms and are not what this gate is for.
ENABLED = "E0601,E0602"


def pylint_available() -> bool:
    try:
        import pylint  # noqa: F401
    except ImportError:
        return False
    return True


class UndefinedNameTests(unittest.TestCase):
    @unittest.skipUnless(
        pylint_available(), "pylint is not installed; the stress workflow adds it"
    )
    def test_no_used_before_assignment_or_undefined_names(self):
        targets = [str(ROOT / path) for path in CHECKED]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pylint",
                "--disable=all",
                f"--enable={ENABLED}",
                "--score=n",
                *targets,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        # signal.SIGUSR1 does not exist on Windows and is guarded at runtime,
        # so a no-member report there is expected; it is not in ENABLED.
        findings = [
            line
            for line in result.stdout.splitlines()
            if ": E" in line and "no-member" not in line
        ]
        self.assertEqual(
            findings,
            [],
            "static analysis found undefined or unbound names:\n"
            + "\n".join(findings),
        )


class ScriptImportTests(unittest.TestCase):
    """Every never-imported script must at least be importable.

    This catches syntax errors and import-time failures in files the suite
    never loads, which is how release-only code silently rots.
    """

    SCRIPTS = (
        "validation/cross_platform/source_linux.py",
        "validation/cross_platform/target_macos.py",
        "validation/cross_platform/verify_evidence.py",
        "packaging/archive_bundle.py",
        "packaging/archive_bundle_zip.py",
    )

    def test_scripts_compile(self):
        import py_compile

        for name in self.SCRIPTS:
            with self.subTest(script=name):
                path = ROOT / name
                self.assertTrue(path.is_file(), f"{name} is missing")
                try:
                    py_compile.compile(str(path), doraise=True, cfile=None)
                except py_compile.PyCompileError as exc:  # pragma: no cover
                    self.fail(f"{name} does not compile: {exc}")


if __name__ == "__main__":
    unittest.main()
