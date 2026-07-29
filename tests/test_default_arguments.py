import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from continuum.compiler import compile_source
from continuum.errors import ExecutionError, FrozenExecution
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine


SOURCE = """
events = []

def make_default():
    events.append("made")
    return []

def collect(value, bucket=make_default()):
    bucket.append(value)
    print("CALL", value, len(bucket))
    return bucket

first = collect(2)
second = collect(value=3)
third = collect(4, [])
print("RESULT", events, first is second, first, third)
"""


def run_cpython(source: str) -> str:
    output = io.StringIO()
    with redirect_stdout(output):
        exec(compile(source, "defaults.py", "exec"), {})
    return output.getvalue()


class DefaultArgumentTests(unittest.TestCase):
    def test_definition_time_defaults_and_mutable_identity_match_cpython(self):
        expected = run_cpython(SOURCE)
        ir = compile_source(SOURCE, "defaults.py")
        vm = VirtualMachine(ir, ["defaults.py"], "defaults.py")
        output = io.StringIO()
        with redirect_stdout(output):
            vm.run()

        self.assertEqual(output.getvalue(), expected)
        self.assertEqual(vm.globals["events"], ["made"])
        self.assertIs(vm.globals["first"], vm.globals["second"])
        self.assertIs(
            vm.globals["collect"].defaults[0],
            vm.globals["first"],
        )
        definition = ir["functions"]["__module__.collect@8"]
        self.assertEqual(definition["default_count"], 1)

    def test_every_default_argument_safe_point_resumes_with_cpython_output(self):
        expected = run_cpython(SOURCE)
        ir = compile_source(SOURCE, "defaults.py")
        counter = VirtualMachine(ir, ["defaults.py"], "defaults.py")
        with redirect_stdout(io.StringIO()):
            counter.run()

        for threshold in range(1, counter.safe_points_executed + 1):
            with self.subTest(safe_point=threshold):
                with tempfile.TemporaryDirectory() as temporary:
                    image = Path(temporary) / "defaults.cont"
                    before = io.StringIO()
                    frozen = False

                    def checkpoint(vm):
                        nonlocal frozen
                        if frozen or vm.safe_points_executed < threshold:
                            return
                        save_image(image, vm, SOURCE)
                        frozen = True
                        raise FrozenExecution

                    source_vm = VirtualMachine(
                        ir,
                        ["defaults.py"],
                        "defaults.py",
                        safe_point_callback=checkpoint,
                    )
                    with redirect_stdout(before):
                        with self.assertRaises(FrozenExecution):
                            source_vm.run()
                    restored = load_image(image).restore_vm()
                    after = io.StringIO()
                    with redirect_stdout(after):
                        restored.run()

                    self.assertEqual(
                        before.getvalue() + after.getvalue(),
                        expected,
                    )
                    self.assertIs(
                        restored.globals["collect"].defaults[0],
                        restored.globals["first"],
                    )

    def test_missing_required_argument_remains_an_error(self):
        source = """
def add(left, right=2):
    return left + right

answer = add()
"""
        vm = VirtualMachine(
            compile_source(source, "missing.py"),
            ["missing.py"],
            "missing.py",
        )
        with self.assertRaisesRegex(ExecutionError, "missing arguments.*left"):
            vm.run()


if __name__ == "__main__":
    unittest.main()
