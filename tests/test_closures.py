"""Lexical closures with real shared cells.

The property under test throughout is identity: two functions closing over one
variable must still share one binding after the graph is encoded, written to an
image, and restored in another process.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from continuum.codec import decode_graph, encode_graph
from continuum.compiler import compile_source
from continuum.errors import CompileError, FrozenExecution, ImageError
from continuum.image import load_image, save_image
from continuum.values import EMPTY, Cell, FunctionValue
from continuum.vm import VirtualMachine

DIFFERENTIAL_CASES = {
    "counter_mutates_through_nonlocal": (
        "def outer():\n"
        "    n = 0\n"
        "    def inc():\n"
        "        nonlocal n\n"
        "        n = n + 1\n"
        "        return n\n"
        "    return inc\n"
        "f = outer()\n"
        "print(f(), f(), f())\n"
    ),
    "siblings_share_one_binding": (
        "def make():\n"
        "    n = 0\n"
        "    def inc():\n"
        "        nonlocal n\n"
        "        n = n + 1\n"
        "        return n\n"
        "    def get():\n"
        "        return n\n"
        "    return [inc, get]\n"
        "pair = make()\n"
        "pair[0]()\n"
        "pair[0]()\n"
        "print(pair[1]())\n"
    ),
    "separate_calls_are_independent": (
        "def make():\n"
        "    n = 0\n"
        "    def inc():\n"
        "        nonlocal n\n"
        "        n = n + 1\n"
        "        return n\n"
        "    return inc\n"
        "a = make()\n"
        "b = make()\n"
        "a()\n"
        "a()\n"
        "b()\n"
        "print(a(), b())\n"
    ),
    "read_only_capture": (
        "def outer(x):\n"
        "    def inner():\n"
        "        return x * 2\n"
        "    return inner\n"
        "print(outer(5)())\n"
    ),
    "capture_two_levels_deep": (
        "def a():\n"
        "    x = 1\n"
        "    def b():\n"
        "        def c():\n"
        "            return x\n"
        "        return c\n"
        "    return b\n"
        "print(a()()())\n"
    ),
    "captured_parameter_is_mutable": (
        "def outer(start):\n"
        "    def bump():\n"
        "        nonlocal start\n"
        "        start = start + 10\n"
        "        return start\n"
        "    return bump\n"
        "b = outer(1)\n"
        "print(b(), b())\n"
    ),
    "captured_mutable_object_is_shared": (
        "def make():\n"
        "    items = []\n"
        "    def add(v):\n"
        "        items.append(v)\n"
        "        return items\n"
        "    def size():\n"
        "        return len(items)\n"
        "    return [add, size]\n"
        "p = make()\n"
        "p[0](1)\n"
        "p[0](2)\n"
        "print(p[1](), p[0](3))\n"
    ),
    "free_name_assigned_after_definition": (
        "def outer():\n"
        "    def inner():\n"
        "        return n\n"
        "    n = 5\n"
        "    return inner()\n"
        "print(outer())\n"
    ),
    "free_name_read_before_assignment": (
        "def outer():\n"
        "    def inner():\n"
        "        return n\n"
        "    try:\n"
        "        return inner()\n"
        "    except NameError:\n"
        "        return 'unbound'\n"
        "print(outer())\n"
    ),
    "closures_from_a_loop_keep_their_own_value": (
        "fns = []\n"
        "for i in range(3):\n"
        "    def make(k):\n"
        "        def f():\n"
        "            return k\n"
        "        return f\n"
        "    fns.append(make(i))\n"
        "print(fns[0](), fns[1](), fns[2]())\n"
    ),
    "closure_and_argument_binding_together": (
        "def outer(base):\n"
        "    def combine(*rest, scale=2, **extra):\n"
        "        nonlocal base\n"
        "        base = base + len(rest) + len(extra)\n"
        "        return base * scale\n"
        "    return combine\n"
        "c = outer(1)\n"
        "print(c(1, 2, tag='x'), c(scale=3))\n"
    ),
}


def run_cpython(source: str) -> tuple[str, str]:
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(compile(source, "<case>", "exec"), {"__name__": "__main__"})
    except BaseException as exc:  # noqa: BLE001 - differential comparison
        return type(exc).__name__, output.getvalue()
    return "ok", output.getvalue()


def run_continuum(source: str) -> tuple[str, str]:
    output = io.StringIO()
    vm = VirtualMachine(compile_source(source, "<case>"), ["<case>"], "<case>")
    try:
        with contextlib.redirect_stdout(output):
            vm.run()
    except BaseException as exc:  # noqa: BLE001 - differential comparison
        cause = exc.__cause__ or exc
        return type(cause).__name__, output.getvalue()
    return "ok", output.getvalue()


class ClosureDifferentialTests(unittest.TestCase):
    def test_closure_semantics_match_cpython(self):
        for name, source in DIFFERENTIAL_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(run_continuum(source), run_cpython(source))

    def test_scope_analysis_marks_cells_and_free_names(self):
        ir = compile_source(DIFFERENTIAL_CASES["counter_mutates_through_nonlocal"], "<c>")
        outer = ir["functions"]["__module__.outer@1"]
        inner = ir["functions"]["outer.inc@3"]
        self.assertEqual(outer["cellvars"], ["n"])
        self.assertEqual(outer["freevars"], [])
        self.assertEqual(inner["cellvars"], [])
        self.assertEqual(inner["freevars"], ["n"])

    def test_nonlocal_without_an_enclosing_binding_is_rejected(self):
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        nonlocal missing\n"
            "        missing = 1\n"
        )
        with self.assertRaises(CompileError):
            compile_source(source, "<case>")

    def test_global_declaration_is_still_rejected(self):
        with self.assertRaises(CompileError):
            compile_source("def f():\n    global v\n    v = 1\n", "<case>")


class CellCodecTests(unittest.TestCase):
    def test_two_functions_keep_one_cell_across_the_codec(self):
        cell = Cell(5)
        first = FunctionValue("__module__.a@1", (), (), (cell,))
        second = FunctionValue("__module__.b@2", (), (), (cell,))
        document = encode_graph({"first": first, "second": second})
        restored = decode_graph(json.loads(json.dumps(document)))

        self.assertIs(restored["first"].closure[0], restored["second"].closure[0])
        restored["first"].closure[0].set(9)
        self.assertEqual(restored["second"].closure[0].value, 9)

    def test_empty_cell_round_trips_as_empty(self):
        document = encode_graph({"cell": Cell()})
        restored = decode_graph(json.loads(json.dumps(document)))
        self.assertTrue(restored["cell"].is_empty())
        with self.assertRaises(NameError):
            restored["cell"].get("value")

    def test_cell_reachable_from_its_own_contents_round_trips(self):
        cell = Cell()
        holder: list = [cell]
        cell.set(holder)
        restored = decode_graph(
            json.loads(json.dumps(encode_graph({"cell": cell})))
        )["cell"]
        self.assertIs(restored.value[0], restored)

    def test_malformed_cell_records_are_rejected(self):
        cases = {
            "missing empty flag": lambda node: node.pop("empty"),
            "non-boolean empty flag": lambda node: node.update({"empty": "yes"}),
            "empty cell carrying a value": lambda node: node.update(
                {"empty": True}
            ),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                document = encode_graph({"cell": Cell(1)})
                for node in document["objects"]:
                    if node.get("kind") == "cell":
                        damage(node)
                with self.assertRaises(ImageError):
                    decode_graph(document)

    def test_non_empty_cell_without_a_value_is_rejected(self):
        document = encode_graph({"cell": Cell(1)})
        for node in document["objects"]:
            if node.get("kind") == "cell":
                node.pop("value")
        with self.assertRaises(ImageError):
            decode_graph(document)

    def test_closure_of_non_cells_is_rejected(self):
        document = encode_graph(
            {"f": FunctionValue("__module__.a@1", (), (), (Cell(1),))}
        )
        for node in document["objects"]:
            if node.get("kind") == "cell":
                node["kind"] = "list"
                node["items"] = []
                node.pop("empty", None)
                node.pop("value", None)
        with self.assertRaises(ImageError):
            decode_graph(document)


class ClosureContinuationTests(unittest.TestCase):
    SOURCE = (
        "def make(tag):\n"
        "    total = 0\n"
        "    def add(value):\n"
        "        nonlocal total\n"
        "        total = total + value\n"
        "        return total\n"
        "    def report():\n"
        "        return (tag, total)\n"
        "    return [add, report]\n"
        "\n"
        "pair = make('run')\n"
        "index = 0\n"
        "while index < 8:\n"
        "    pair[0](index)\n"
        "    index = index + 1\n"
        "print(pair[1]())\n"
    )

    def _freeze_inside_closure(self):
        def callback(vm):
            frame = vm.frames[-1]
            if frame.function_id.endswith("add@3") and frame.cells:
                raise FrozenExecution

        vm = VirtualMachine(
            compile_source(self.SOURCE, "<case>"),
            ["<case>"],
            "<case>",
            safe_point_callback=callback,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(FrozenExecution):
                vm.run()
        return vm, output.getvalue()

    def test_shared_cell_survives_an_image_and_stays_shared(self):
        vm, before = self._freeze_inside_closure()
        self.assertIn("total", vm.frames[-1].cells)

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "closure.cont"
            save_image(str(image), vm, self.SOURCE)
            loaded = load_image(str(image))
            loaded.validate_compatibility()
            resumed = loaded.restore_vm(None, {})
            after = io.StringIO()
            with contextlib.redirect_stdout(after):
                resumed.run()

        control_vm = VirtualMachine(
            compile_source(self.SOURCE, "<case>"), ["<case>"], "<case>"
        )
        control = io.StringIO()
        with contextlib.redirect_stdout(control):
            control_vm.run()

        # If the resumed image had copied the cell instead of sharing it, the
        # reporting closure would disagree with the accumulating one and the
        # combined output would drift from the control.
        self.assertEqual(before + after.getvalue(), control.getvalue())

    def test_restored_closures_still_point_at_one_cell(self):
        vm, _ = self._freeze_inside_closure()
        restored = decode_graph(
            json.loads(json.dumps(encode_graph(vm.state_root())))
        )
        pair = restored["globals"]["pair"]
        self.assertIsInstance(pair[0], FunctionValue)
        shared = {
            id(cell) for function in pair for cell in function.closure
        }
        # `total` is captured by both; `tag` only by the reporter.
        self.assertEqual(len(shared), 2)
        by_name = dict(zip(("add", "report"), pair))
        common = set(map(id, by_name["add"].closure)) & set(
            map(id, by_name["report"].closure)
        )
        self.assertEqual(len(common), 1)

    def test_restore_rejects_a_frame_whose_cell_is_not_a_cell(self):
        vm, _ = self._freeze_inside_closure()
        vm.frames[-1].cells["total"] = 5
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "bad.cont"
            save_image(str(image), vm, self.SOURCE)
            loaded = load_image(str(image))
            loaded.validate_compatibility()
            with self.assertRaises(ImageError):
                loaded.restore_vm(None, {})

    def test_restore_rejects_a_frame_missing_a_closed_over_binding(self):
        vm, _ = self._freeze_inside_closure()
        vm.frames[-1].cells.pop("total")
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "missing.cont"
            save_image(str(image), vm, self.SOURCE)
            loaded = load_image(str(image))
            loaded.validate_compatibility()
            with self.assertRaises(ImageError):
                loaded.restore_vm(None, {})


if __name__ == "__main__":
    unittest.main()
