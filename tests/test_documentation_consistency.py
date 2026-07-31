"""Fail the build when documentation disagrees with the runtime.

Every check here exists because the disagreement it detects actually shipped:
a released tree published a stale compatibility rate, a language matrix that
named the wrong IR revision, and rows claiming constructs were rejected that
the compiler accepts. Documentation drift is a trust problem, so it is a test
failure rather than a review comment.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from continuum import FORMAT_VERSION, IR_VERSION, SUPPORTED_PYTHON, __version__
from continuum.compiler import compile_source
from continuum.errors import CompileError

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class VersionConsistencyTests(unittest.TestCase):
    def test_package_metadata_matches_the_runtime(self):
        pyproject = read("pyproject.toml")
        self.assertIn(f'version = "{__version__}"', pyproject)
        self.assertIn(f'requires-python = "=={SUPPORTED_PYTHON}"', pyproject)

    def test_readme_version_badge_matches_the_runtime(self):
        badge = re.search(r"badge/version-([0-9a-z.]+)-", read("README.md"))
        self.assertIsNotNone(badge, "README has no version badge")
        self.assertEqual(badge.group(1), __version__)

    def test_status_header_matches_the_runtime(self):
        header = re.search(
            r"Version: ([0-9a-z.]+) · IR ([0-9.]+) · image format ([0-9.]+)",
            read("STATUS.md"),
        )
        self.assertIsNotNone(header, "STATUS.md has no version header")
        self.assertEqual(
            header.groups(), (__version__, IR_VERSION, FORMAT_VERSION)
        )

    def test_format_contract_names_the_shipping_versions(self):
        format_doc = read("FORMAT.md")
        self.assertIn(f"- IR {IR_VERSION};", format_doc)
        self.assertIn(f"- Continuum runtime {__version__};", format_doc)

    def test_portability_names_the_shipping_runtime(self):
        self.assertIn(
            f"exact runtime version `{__version__}`", read("PORTABILITY.md")
        )

    def test_language_matrix_names_the_shipping_ir(self):
        self.assertIn(
            f"Continuum IR {IR_VERSION} (runtime {__version__})",
            read("LANGUAGE_SUPPORT.md"),
        )


class TestCountConsistencyTests(unittest.TestCase):
    """The published test count must equal the count actually discovered."""

    def discovered(self) -> int:
        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        self.assertEqual(loader.errors, [], "test discovery reported errors")

        def count(item) -> int:
            if isinstance(item, unittest.TestSuite):
                return sum(count(child) for child in item)
            return 1

        return count(suite)

    def test_documented_counts_match_discovery(self):
        actual = self.discovered()
        for name in ("README.md", "STATUS.md", "docs/TESTING.md", "ROADMAP.md"):
            for claimed in re.findall(r"\b(\d{2,4}) tests\b", read(name)):
                with self.subTest(document=name, claimed=claimed):
                    self.assertEqual(
                        int(claimed),
                        actual,
                        f"{name} claims {claimed} tests; discovery finds {actual}",
                    )


class LanguageMatrixAccuracyTests(unittest.TestCase):
    """A matrix row must agree with what the compiler actually does."""

    # (row label, source, compiles?)
    CASES = (
        ("Starred / double-star call arguments", "def f(a, b):\n    return a\nf(*[1, 2])\n", True),
        ("Starred / double-star call arguments", "def f(a, b):\n    return a\nf(**{'a': 1, 'b': 2})\n", True),
        ("F-strings", "v = 1\nx = f'{v}'\n", True),
        ("F-string conversions (`!r`, `!s`, `!a`)", "v = 1\nx = f'{v!r}'\n", False),
        ("Comprehensions / nested comprehensions", "x = [i for i in range(3)]\n", False),
        ("Generator expressions / generators / `yield`", "def g():\n    yield 1\n", False),
        ("`with` / context managers", "with open('x') as h:\n    pass\n", False),
        ("Chained comparisons", "x = 1 < 2 < 3\n", False),
        ("Chained assignment", "a = b = 1\n", False),
        ("Lambda", "f = lambda x: x\n", False),
        ("Decorators", "def d(f):\n    return f\n\n@d\ndef g():\n    return 1\n", False),
        ("`global`", "def f():\n    global v\n    v = 1\n", False),
        ("Inheritance, base classes, metaclasses, class decorators", "class C(dict):\n    pass\n", False),
    )

    def compiles(self, source: str) -> bool:
        try:
            compile_source(source, "<matrix>")
        except CompileError:
            return False
        return True

    def test_matrix_rows_match_compiler_behavior(self):
        matrix = read("LANGUAGE_SUPPORT.md")
        for label, source, expected in self.CASES:
            with self.subTest(row=label):
                row = next(
                    (
                        line
                        for line in matrix.splitlines()
                        if line.startswith(f"| {label} |")
                    ),
                    None,
                )
                self.assertIsNotNone(row, f"no matrix row for {label!r}")
                rejected = "explicitly rejected" in row
                actual = self.compiles(source)
                self.assertEqual(
                    actual,
                    expected,
                    f"compiler behavior for {label!r} changed; update the case",
                )
                self.assertEqual(
                    rejected,
                    not actual,
                    f"{label!r} row says "
                    f"{'rejected' if rejected else 'supported'} but the "
                    f"compiler {'accepts' if actual else 'rejects'} it",
                )


class PlatformClaimTests(unittest.TestCase):
    """Windows cross-platform continuation has never been run."""

    DOCS = (
        "README.md",
        "STATUS.md",
        "PORTABILITY.md",
        "LIMITATIONS.md",
        "docs/TESTING.md",
        "LANGUAGE_SUPPORT.md",
        "COMPATIBILITY.md",
    )

    def test_no_document_claims_a_verified_windows_migration(self):
        pattern = re.compile(
            r"(verified|proven|proof)[^.\n]{0,80}windows[^.\n]{0,40}"
            r"(->|→|to )\s*(linux|macos)",
            re.IGNORECASE,
        )
        reverse = re.compile(
            r"(verified|proven|proof)[^.\n]{0,80}(linux|macos)[^.\n]{0,40}"
            r"(->|→|to )\s*windows",
            re.IGNORECASE,
        )
        for name in self.DOCS:
            text = read(name)
            for expression in (pattern, reverse):
                with self.subTest(document=name):
                    self.assertIsNone(
                        expression.search(text),
                        f"{name} appears to claim a verified Windows "
                        "cross-platform path; none has been run",
                    )

    def test_cross_platform_workflow_still_has_no_windows_job(self):
        # The guard above is only meaningful while this remains true.
        workflow = read(".github/workflows/cross-platform-proof.yml")
        self.assertNotIn("windows", workflow.lower())


if __name__ == "__main__":
    unittest.main()
