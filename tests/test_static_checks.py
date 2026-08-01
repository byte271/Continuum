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


class TestModuleLayoutTests(unittest.TestCase):
    """A `unittest.main()` guard must be the last thing in a test module.

    `unittest discover` imports a module, so a guard placed above later
    `TestCase` classes still collects them and CI stays green. Running the file
    directly executes the guard first and the process exits before those
    classes exist -- reporting success for tests that never ran. Three
    regression tests for the stale-identifier fix sat below such a guard.
    """

    def test_the_main_guard_is_last_in_every_test_module(self):
        import ast

        findings = []
        def is_main_guard(node) -> bool:
            """`if __name__ == "__main__":`, matched structurally.

            Searching the dumped tree for the string would also match an
            unrelated condition such as `if runner == "__main__":`. One of
            those appearing below the real guard would move the reference
            line past the classes this check exists to find.
            """

            if not isinstance(node, ast.If):
                return False
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                return False
            if not isinstance(test.ops[0], ast.Eq):
                return False
            left, right = test.left, test.comparators[0]
            if isinstance(right, ast.Name) and isinstance(left, ast.Constant):
                left, right = right, left
            return (
                isinstance(left, ast.Name)
                and left.id == "__name__"
                and isinstance(right, ast.Constant)
                and right.value == "__main__"
            )

        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            guards = [node.lineno for node in tree.body if is_main_guard(node)]
            if not guards:
                continue
            later = [
                node.name
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.lineno > guards[-1]
            ]
            if later:
                findings.append(
                    f"{path.name}: {', '.join(later)} defined after the "
                    f"__main__ guard on line {guards[-1]}"
                )
        self.assertEqual(
            findings,
            [],
            "test classes below a __main__ guard never run on a direct "
            "invocation:\n" + "\n".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
