from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from continuum.compiler import compile_source
from continuum.errors import ExecutionError, FrozenExecution
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine


PROGRAM = """
def leaf(limit, seen):
    index = 0
    local_token = "alive-in-leaf"
    while index < limit:
        seen.append(index)
        print("STEP", index)
        index += 1
    return len(seen)

def middle(limit, seen):
    middle_token = "alive-in-middle"
    return leaf(limit, seen)

def outer(limit):
    shared = []
    graph = {"left": shared, "right": shared}
    graph["self"] = graph
    print("START_SENTINEL")
    answer = middle(limit, shared)
    print("IDENTITY", graph["left"] is graph["right"], graph["self"] is graph)
    print("DONE", answer)
    return answer

module_value = "module-state"
result = outer(20)
"""


class PredicateCheckpoint:
    def __init__(self, image: Path, source: str, predicate):
        self.image = image
        self.source = source
        self.predicate = predicate
        self.did_freeze = False

    def __call__(self, vm):
        if self.did_freeze or not self.predicate(vm):
            return
        save_image(self.image, vm, self.source)
        self.did_freeze = True
        raise FrozenExecution


def run_program(source: str) -> tuple[str, VirtualMachine]:
    ir = compile_source(source, "test_program.py")
    vm = VirtualMachine(ir, ["test_program.py"], "test_program.py")
    output = io.StringIO()
    with redirect_stdout(output):
        vm.run()
    return output.getvalue(), vm


class ContinuationTests(unittest.TestCase):
    def test_nested_frames_locals_position_and_no_restart(self):
        control_output, control_vm = run_program(PROGRAM)
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "state.cont"

            def inside_leaf(vm):
                if len(vm.frames) < 4:
                    return False
                frame = vm.frames[-1]
                return (
                    vm.ir["functions"][frame.function_id]["name"] == "leaf"
                    and frame.locals.get("index") == 7
                )

            checkpoint = PredicateCheckpoint(image, PROGRAM, inside_leaf)
            ir = compile_source(PROGRAM, "test_program.py")
            source_vm = VirtualMachine(
                ir,
                ["test_program.py"],
                "test_program.py",
                safe_point_callback=checkpoint,
            )
            before = io.StringIO()
            with redirect_stdout(before):
                with self.assertRaises(FrozenExecution):
                    source_vm.run()
            self.assertTrue(checkpoint.did_freeze)
            self.assertEqual(len(source_vm.frames), 4)
            self.assertEqual(source_vm.frames[-1].locals["local_token"], "alive-in-leaf")
            self.assertEqual(source_vm.frames[-2].locals["middle_token"], "alive-in-middle")

            loaded = load_image(image)
            resumed_vm = loaded.restore_vm()
            self.assertEqual(resumed_vm.globals["module_value"], "module-state")
            after = io.StringIO()
            with redirect_stdout(after):
                resumed_vm.run()

            combined = before.getvalue() + after.getvalue()
            self.assertEqual(control_output, combined)
            self.assertNotIn("START_SENTINEL", after.getvalue())
            self.assertEqual(combined.count("START_SENTINEL"), 1)
            self.assertEqual(combined.count("STEP 6\n"), 1)
            self.assertEqual(resumed_vm.globals["result"], control_vm.globals["result"])

    def test_shared_identity_and_cycle_are_live_after_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "state.cont"

            def predicate(vm):
                if len(vm.frames) < 2 or "graph" not in vm.frames[-1].locals:
                    return False
                graph = vm.frames[-1].locals["graph"]
                return graph.get("self") is graph

            checkpoint = PredicateCheckpoint(image, PROGRAM, predicate)
            ir = compile_source(PROGRAM, "test_program.py")
            vm = VirtualMachine(
                ir,
                ["test_program.py"],
                "test_program.py",
                safe_point_callback=checkpoint,
            )
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(FrozenExecution):
                    vm.run()
            restored = load_image(image).restore_vm()
            graph = restored.frames[-1].locals["graph"]
            self.assertIs(graph["left"], graph["right"])
            self.assertIs(graph["self"], graph)

    def test_try_finally_control_state_survives(self):
        source = """
def work():
    trace = []
    value = 0
    try:
        value = 41
    finally:
        trace.append("finally")
        value += 1
    print("TRACE", trace[0], value)
    return value

answer = work()
"""
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "finally.cont"

            def in_finally(vm):
                return bool(vm.frames[-1].finally_reasons)

            checkpoint = PredicateCheckpoint(image, source, in_finally)
            vm = VirtualMachine(
                compile_source(source, "finally.py"),
                ["finally.py"],
                "finally.py",
                safe_point_callback=checkpoint,
            )
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(FrozenExecution):
                    vm.run()
            resumed = load_image(image).restore_vm()
            output = io.StringIO()
            with redirect_stdout(output):
                resumed.run()
            self.assertEqual(resumed.globals["answer"], 42)
            self.assertIn("TRACE finally 42", output.getvalue())

    def test_pending_exception_survives_inside_finally(self):
        source = """
trace = []

def work():
    try:
        raise ValueError("boom")
    finally:
        trace.append("finally")

work()
"""
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "exception.cont"

            def pending_exception(vm):
                reasons = vm.frames[-1].finally_reasons
                return bool(reasons) and reasons[-1]["kind"] == "exception"

            checkpoint = PredicateCheckpoint(image, source, pending_exception)
            vm = VirtualMachine(
                compile_source(source, "exception.py"),
                ["exception.py"],
                "exception.py",
                safe_point_callback=checkpoint,
            )
            with self.assertRaises(FrozenExecution):
                vm.run()
            resumed = load_image(image).restore_vm()
            with self.assertRaisesRegex(ExecutionError, "unhandled ValueError"):
                resumed.run()
            self.assertEqual(resumed.globals["trace"], ["finally"])

    def test_partial_caller_operand_stack_survives_nested_call(self):
        source = """
def left():
    return 10

def leaf():
    index = 0
    while index < 8:
        index += 1
    return 5

answer = left() + leaf()
"""
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "operand-stack.cont"

            def inside_leaf(vm):
                return (
                    len(vm.frames) == 2
                    and vm.ir["functions"][vm.frames[-1].function_id]["name"]
                    == "leaf"
                    and vm.frames[-1].locals.get("index") == 3
                )

            checkpoint = PredicateCheckpoint(image, source, inside_leaf)
            vm = VirtualMachine(
                compile_source(source, "operand_stack.py"),
                ["operand_stack.py"],
                "operand_stack.py",
                safe_point_callback=checkpoint,
            )
            with self.assertRaises(FrozenExecution):
                vm.run()
            self.assertEqual(vm.frames[0].stack, [10])
            restored = load_image(image).restore_vm()
            self.assertEqual(restored.frames[0].stack, [10])
            restored.run()
            self.assertEqual(restored.globals["answer"], 15)

    def test_freeze_between_iterator_advance_and_loop_body(self):
        source = """
events = []
for item in [1, 2, 3]:
    events.append(item)
"""
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "iterator-boundary.cont"

            def after_advance(vm):
                return (
                    vm.frames[-1].locals.get("item") == 2
                    and vm.globals["events"] == [1]
                )

            checkpoint = PredicateCheckpoint(image, source, after_advance)
            vm = VirtualMachine(
                compile_source(source, "iterator_boundary.py"),
                ["iterator_boundary.py"],
                "iterator_boundary.py",
                safe_point_callback=checkpoint,
            )
            with self.assertRaises(FrozenExecution):
                vm.run()
            restored = load_image(image).restore_vm()
            restored.run()
            self.assertEqual(restored.globals["events"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
