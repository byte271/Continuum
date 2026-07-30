from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from continuum.compiler import compile_source
from continuum.errors import CompileError, ExecutionError, ImageError
from continuum.image import save_image
from continuum.vm import VirtualMachine


def run_source(source: str) -> VirtualMachine:
    vm = VirtualMachine(
        compile_source(source, "semantic_audit.py"),
        ["semantic_audit.py"],
        "semantic_audit.py",
    )
    vm.run()
    return vm


class SemanticAuditTests(unittest.TestCase):
    def test_assignment_rhs_precedes_subscript_target_evaluation(self):
        source = """
events = []
target = {}

def rhs():
    events.append("rhs")
    return 7

def key():
    events.append("key")
    return "answer"

target[key()] = rhs()
"""
        vm = run_source(source)
        self.assertEqual(vm.globals["events"], ["rhs", "key"])
        self.assertEqual(vm.globals["target"], {"answer": 7})

    def test_closure_capture_runs_with_correct_scoping(self):
        # This construct was previously rejected so it could not run with the
        # wrong semantics. It now runs, so the audit asserts the semantics.
        source = """
def outer():
    captured = 41
    def inner():
        return captured + 1
    return inner()

answer = outer()
"""
        vm = VirtualMachine(
            compile_source(source, "closure.py"), ["closure.py"], "closure.py"
        )
        vm.run()
        self.assertEqual(vm.globals["answer"], 42)

    def test_captured_binding_is_shared_not_copied(self):
        source = """
def outer():
    total = 0
    def add(value):
        nonlocal total
        total = total + value
        return total
    add(2)
    add(3)
    return total

answer = outer()
"""
        vm = VirtualMachine(
            compile_source(source, "shared.py"), ["shared.py"], "shared.py"
        )
        vm.run()
        # A copied binding would leave the enclosing frame reading 0.
        self.assertEqual(vm.globals["answer"], 5)

    def test_attribute_assignment_on_a_host_object_is_still_refused(self):
        # Attribute assignment now compiles, but only a VM-owned instance can
        # hold the result. Writing onto a host object would create state the
        # image cannot represent, so it fails at the write.
        source = """
import random
rng = random.Random(1)
rng.extra_state = 9
"""
        vm = VirtualMachine(
            compile_source(source, "attribute.py"),
            ["attribute.py"],
            "attribute.py",
        )
        with self.assertRaises(ExecutionError) as caught:
            vm.run()
        self.assertIn("cannot set attribute", str(caught.exception))

    def test_instance_attribute_assignment_is_preserved(self):
        source = """
class Holder:
    def __init__(self):
        self.value = 0

holder = Holder()
holder.value = 7
holder.extra = [1, 2]
"""
        vm = VirtualMachine(
            compile_source(source, "instance.py"),
            ["instance.py"],
            "instance.py",
        )
        vm.run()
        holder = vm.globals["holder"]
        self.assertEqual(holder.attributes["value"], 7)
        self.assertEqual(holder.attributes["extra"], [1, 2])

    def test_checkpoint_rejects_unresumable_wrapper_cycle_before_commit(self):
        source = """
def work():
    index = 0
    while index < 10:
        index += 1
    return index

answer = work()
"""
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "must-not-commit.cont"
            vm = VirtualMachine(
                compile_source(source, "immutable_cycle.py"),
                ["immutable_cycle.py"],
                "immutable_cycle.py",
            )
            while (
                len(vm.frames) < 2
                or vm.frames[-1].locals.get("index") != 3
            ):
                vm.step()
            error = RuntimeError("self-referential")
            error.args = (error,)
            vm.globals["unresumable"] = error
            with self.assertRaisesRegex(
                ImageError, "immutable or wrapper object"
            ):
                save_image(image, vm, source)
            self.assertFalse(image.exists())
            del vm.globals["unresumable"]
            vm.run()
            self.assertEqual(vm.globals["answer"], 10)

    def test_function_local_does_not_fall_back_to_same_named_global(self):
        source = """
value = "global"

def read_before_assignment():
    observed = value
    value = "local"
    return observed

answer = read_before_assignment()
"""
        with self.assertRaisesRegex(ExecutionError, "UnboundLocalError"):
            run_source(source)

    def test_break_discards_the_for_iterator_operand(self):
        source = """
def work():
    for item in [1, 2, 3]:
        break
    marker = 1
    while marker < 20:
        marker += 1
    return marker

answer = work()
"""
        vm = VirtualMachine(
            compile_source(source, "break_stack.py"),
            ["break_stack.py"],
            "break_stack.py",
        )
        while (
            len(vm.frames) < 2
            or vm.frames[-1].locals.get("marker") != 1
        ):
            vm.step()
        self.assertEqual(vm.frames[-1].stack, [])
        vm.run()
        self.assertEqual(vm.globals["answer"], 20)

    def test_continue_only_loop_still_has_freeze_safe_points(self):
        source = """
index = 0
while index < 10:
    index += 1
    continue
"""
        vm = VirtualMachine(
            compile_source(source, "continue.py"),
            ["continue.py"],
            "continue.py",
        )
        vm.run()
        self.assertGreaterEqual(vm.safe_points_executed, 10)

    def test_loop_else_runs_only_on_normal_exhaustion(self):
        source = """
events = []

for value in [1, 2]:
    events.append(value)
else:
    events.append("for-exhausted")

for value in [1, 2]:
    break
else:
    events.append("wrong-for-else")

index = 0
while index < 2:
    index += 1
else:
    events.append("while-exhausted")

while True:
    break
else:
    events.append("wrong-while-else")
"""
        vm = run_source(source)
        self.assertEqual(
            vm.globals["events"],
            [1, 2, "for-exhausted", "while-exhausted"],
        )


if __name__ == "__main__":
    unittest.main()
