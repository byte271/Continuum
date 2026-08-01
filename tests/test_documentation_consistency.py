"""Fail the build when documentation disagrees with the runtime.

Every check here exists because the disagreement it detects actually shipped:
a released tree published a stale compatibility rate, a language matrix that
named the wrong IR revision, and rows claiming constructs were rejected that
the compiler accepts. Documentation drift is a trust problem, so it is a test
failure rather than a review comment.
"""

from __future__ import annotations

import platform
import re
import unittest
from pathlib import Path
from unittest import mock

from continuum import FORMAT_VERSION, IR_VERSION, SUPPORTED_PYTHON, __version__, abi
from continuum.cli import _require_runtime_version
from continuum.compiler import compile_source
from continuum.errors import CompileError, ContinuumError

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def requires_python() -> list[tuple[str, tuple[int, ...]]]:
    """Parse the project's requires-python into comparable clauses.

    Deliberately hand-rolled: Continuum has no runtime or test dependencies,
    and pulling in a packaging library to read one field would add one.
    """

    match = re.search(r'^requires-python = "([^"]+)"', read("pyproject.toml"), re.M)
    assert match is not None, "pyproject has no requires-python"
    clauses = []
    for part in match.group(1).split(","):
        clause = part.strip()
        operator = re.match(r"^(>=|<=|==|<|>)", clause)
        assert operator is not None, f"unsupported specifier clause {clause!r}"
        symbol = operator.group(1)
        clauses.append((symbol, version_tuple(clause[len(symbol) :])))
    return clauses


def admitted_by_requires_python(value: str) -> bool:
    candidate = version_tuple(value)
    for symbol, bound in requires_python():
        if symbol == ">=" and not candidate >= bound:
            return False
        if symbol == ">" and not candidate > bound:
            return False
        if symbol == "<=" and not candidate <= bound:
            return False
        if symbol == "<" and not candidate < bound:
            return False
        if symbol == "==" and candidate != bound:
            return False
    return True


class VersionConsistencyTests(unittest.TestCase):
    def test_package_metadata_matches_the_runtime(self):
        pyproject = read("pyproject.toml")
        self.assertIn(f'version = "{__version__}"', pyproject)

    def test_requires_python_admits_every_verified_interpreter(self):
        """Packaging metadata must not exclude an interpreter CI has proven.

        This replaces an equality check against one hard-coded version. It is
        strictly stronger: it requires the specifier to admit every verified
        version, and the companion test below requires the runtime to refuse a
        version the specifier admits but nobody verified. An exact allowlist
        cannot be written as a PEP 440 specifier, so the two halves are tested
        separately rather than pretending one field can express both.
        """

        for version in abi.VERIFIED_PYTHON_VERSIONS:
            with self.subTest(version=version):
                self.assertTrue(
                    admitted_by_requires_python(version),
                    f"requires-python excludes verified Python {version}",
                )
        self.assertTrue(admitted_by_requires_python(SUPPORTED_PYTHON))

    def test_runtime_refuses_a_version_packaging_would_admit(self):
        """The runtime allowlist, not requires-python, is the authority."""

        # A real interpreter inside the install range that CI has never proven.
        unverified = "3.13.0"
        self.assertTrue(admitted_by_requires_python(unverified))
        self.assertNotIn(unverified, abi.VERIFIED_PYTHON_VERSIONS)
        with mock.patch.object(
            platform, "python_version", return_value=unverified
        ):
            with self.assertRaises(ContinuumError) as caught:
                _require_runtime_version()
        self.assertIn("has not verified", str(caught.exception))

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
