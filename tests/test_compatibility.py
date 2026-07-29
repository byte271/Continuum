import unittest
from pathlib import Path

from compatibility.runner import PROGRAMS, run_case


class CompatibilityCorpusTests(unittest.TestCase):
    def test_corpus_contains_fifty_unchanged_programs(self):
        programs = sorted(PROGRAMS.glob("*.py"))
        self.assertEqual(len(programs), 50)
        self.assertEqual(len({program.stem for program in programs}), 50)
        for program in programs:
            self.assertGreater(program.stat().st_size, 0)

    def test_supported_program_matches_after_new_process_resume(self):
        result = run_case(PROGRAMS / "text_word_frequency.py")
        self.assertEqual(result["compile"], "passed")
        self.assertEqual(result["run"], "passed")
        self.assertEqual(result["same_process"], "passed")
        self.assertEqual(result["new_process"], "passed")
        self.assertTrue(result["cp_python_output_match"])

    def test_unsupported_program_remains_in_corpus_results(self):
        result = run_case(PROGRAMS / "comprehension_list.py")
        self.assertEqual(result["compile"], "failed")
        self.assertEqual(result["run"], "not_run")
        self.assertEqual(result["failure"]["stage"], "compile")
        self.assertIn("ListComp", result["failure"]["message"])


if __name__ == "__main__":
    unittest.main()
