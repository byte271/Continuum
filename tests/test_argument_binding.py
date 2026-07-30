"""Complete argument binding: semantics, portability, and continuation.

Binding rules and their error messages are compared against CPython running
the identical source, so a divergence in either is a failure.
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
from continuum.values import FunctionValue
from continuum.vm import VirtualMachine

DIFFERENTIAL_CASES = {
    "vararg_collects_a_tuple": (
        "def f(*args):\n    return args\nprint(f(1, 2, 3), f())\n"
    ),
    "kwarg_collects_a_dict": (
        "def f(**kw):\n"
        "    return sorted(kw.items())\n"
        "print(f(a=1, b=2), f())\n"
    ),
    "every_parameter_kind_at_once": (
        "def f(a, b=2, *rest, c, d=4, **kw):\n"
        "    return (a, b, rest, c, d, sorted(kw.items()))\n"
        "print(f(1, 2, 3, 4, c=5, e=6))\n"
    ),
    "positional_only_boundary": (
        "def f(a, b, /, c, *, d):\n"
        "    return (a, b, c, d)\n"
        "print(f(1, 2, 3, d=4))\n"
        "print(f(1, 2, c=3, d=4))\n"
    ),
    "positional_only_name_lands_in_kwargs": (
        "def f(a, /, **kw):\n"
        "    return (a, sorted(kw.items()))\n"
        "print(f(1, a=2))\n"
    ),
    "keyword_only_default": (
        "def f(a, *, b=10):\n    return a + b\nprint(f(1), f(1, b=2))\n"
    ),
    "call_with_star_args": (
        "def f(a, b, c):\n"
        "    return a + b + c\n"
        "values = [1, 2, 3]\n"
        "print(f(*values))\n"
    ),
    "call_with_double_star": (
        "def f(a, b):\n"
        "    return a - b\n"
        "mapping = {'a': 5, 'b': 2}\n"
        "print(f(**mapping))\n"
    ),
    "call_mixes_plain_star_and_keyword": (
        "def f(a, b, c, d):\n"
        "    return (a, b, c, d)\n"
        "print(f(1, *[2, 3], d=4))\n"
    ),
    "star_unpacking_evaluates_left_to_right": (
        "order = []\n"
        "def note(value):\n"
        "    order.append(value)\n"
        "    return value\n"
        "def f(a, b, c, d):\n"
        "    return (a, b, c, d)\n"
        "print(f(note(1), *[note(2)], note(3), d=note(4)))\n"
        "print(order)\n"
    ),
    "vararg_is_a_real_tuple": (
        "def f(*args):\n"
        "    return (len(args), args[0], tuple(args) == args)\n"
        "print(f(7, 8))\n"
    ),
    "defaults_are_shared_across_calls": (
        "def f(x, acc=[]):\n"
        "    acc.append(x)\n"
        "    return acc\n"
        "print(f(1))\n"
        "print(f(2))\n"
    ),
    "keyword_default_is_shared_across_calls": (
        "def f(x, *, acc=[]):\n"
        "    acc.append(x)\n"
        "    return acc\n"
        "print(f(1))\n"
        "print(f(2))\n"
    ),
}

# Binding failures whose CPython message must be reproduced exactly.
ERROR_CASES = {
    "unexpected_keyword": "def f(a):\n    return a\nf(z=1)\n",
    "multiple_values": "def f(a):\n    return a\nf(1, a=2)\n",
    "missing_one_positional": "def f(a, b, c):\n    return a\nf(1, 2)\n",
    "missing_two_positional": "def f(a, b, c):\n    return a\nf(1)\n",
    "missing_keyword_only": "def f(*, a, b):\n    return a\nf()\n",
    "too_many_positional": "def f(a, b):\n    return a\nf(1, 2, 3)\n",
    "too_many_with_defaults": "def f(a, b=1):\n    return a\nf(1, 2, 3)\n",
    "positional_only_by_keyword": "def f(a, /):\n    return a\nf(a=1)\n",
    "duplicate_keyword_from_merge": (
        "def f(**kw):\n    return kw\nf(a=1, **{'a': 2})\n"
    ),
    "duplicate_keyword_nested_callee": (
        "def outer():\n"
        "    def inner(**kw):\n"
        "        return kw\n"
        "    return inner(a=1, **{'a': 2})\n"
        "outer()\n"
    ),
    "star_argument_is_not_iterable": (
        "def f(a):\n    return a\nf(*5)\n"
    ),
    "double_star_argument_is_not_a_mapping": (
        "def f(a):\n    return a\nf(**5)\n"
    ),
}


def run_cpython(source: str) -> tuple[str, str, str]:
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(compile(source, "<case>", "exec"), {"__name__": "__main__"})
    except BaseException as exc:  # noqa: BLE001 - differential comparison
        return type(exc).__name__, str(exc), output.getvalue()
    return "ok", "", output.getvalue()


def run_continuum(source: str) -> tuple[str, str, str]:
    output = io.StringIO()
    vm = VirtualMachine(compile_source(source, "<case>"), ["<case>"], "<case>")
    try:
        with contextlib.redirect_stdout(output):
            vm.run()
    except BaseException as exc:  # noqa: BLE001 - differential comparison
        cause = exc.__cause__ or exc
        return type(cause).__name__, str(cause), output.getvalue()
    return "ok", "", output.getvalue()


class ArgumentBindingDifferentialTests(unittest.TestCase):
    def test_binding_matches_cpython(self):
        for name, source in DIFFERENTIAL_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(run_continuum(source), run_cpython(source))

    def test_binding_errors_match_cpython_exactly(self):
        for name, source in ERROR_CASES.items():
            with self.subTest(case=name):
                expected = run_cpython(source)
                self.assertNotEqual(expected[0], "ok", "case must fail")
                self.assertEqual(run_continuum(source), expected)

    def test_duplicate_parameter_names_are_rejected(self):
        with self.assertRaises(CompileError):
            compile_source("def f(a, *, a):\n    return a\n", "<case>")


class FunctionValueCodecTests(unittest.TestCase):
    def test_keyword_defaults_round_trip_and_keep_identity(self):
        shared: list[int] = []
        function = FunctionValue("__module__.f@1", (shared,), (shared,))
        document = encode_graph({"f": function})
        restored = decode_graph(json.loads(json.dumps(document)))["f"]

        self.assertEqual(restored.function_id, "__module__.f@1")
        # The same mutable default reached through both paths must stay one
        # object, or a resumed call would mutate a copy.
        self.assertIs(restored.defaults[0], restored.kw_defaults[0])

    def test_function_record_without_keyword_defaults_is_rejected(self):
        document = encode_graph({"f": FunctionValue("__module__.f@1", (), ())})
        for node in document["objects"]:
            node.pop("kw_defaults", None)
        with self.assertRaises(ImageError):
            decode_graph(document)

    def test_keyword_defaults_must_decode_to_a_tuple(self):
        document = encode_graph({"f": FunctionValue("__module__.f@1", (), ())})
        for node in document["objects"]:
            if node.get("kind") == "function":
                node["kw_defaults"] = {"t": "int", "v": "3"}
        with self.assertRaises(ImageError):
            decode_graph(document)

    def test_mutable_keyword_default_survives_an_image(self):
        source = (
            "def collect(value, *, acc=[]):\n"
            "    acc.append(value)\n"
            "    return acc\n"
            "collect(1)\n"
            "print(collect(2))\n"
        )
        vm = VirtualMachine(compile_source(source, "<case>"), ["<c>"], "<c>")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            vm.run()
        self.assertEqual(output.getvalue(), "[1, 2]\n")


class ArgumentBindingContinuationTests(unittest.TestCase):
    SOURCE = (
        "log = []\n"
        "def gather(first, *rest, tag='t', **extra):\n"
        "    total = first\n"
        "    for value in rest:\n"
        "        total = total + value\n"
        "    log.append((tag, total, sorted(extra.items())))\n"
        "    return total\n"
        "\n"
        "index = 0\n"
        "while index < 6:\n"
        "    gather(index, index + 1, index + 2, tag='n', extra=index)\n"
        "    index = index + 1\n"
        "print(len(log), log[0], log[5])\n"
    )

    def _freeze_inside_call(self):
        captured: dict[str, object] = {}

        def callback(vm):
            if "done" in captured:
                return
            frame = vm.frames[-1]
            if frame.function_id != "__module__" and any(
                isinstance(value, tuple) for value in frame.locals.values()
            ):
                captured["done"] = True
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

    def test_frame_holding_vararg_and_kwarg_resumes_identically(self):
        vm, before = self._freeze_inside_call()
        frame = vm.frames[-1]
        self.assertIsInstance(frame.locals["rest"], tuple)
        self.assertIsInstance(frame.locals["extra"], dict)

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "binding.cont"
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

        self.assertEqual(before + after.getvalue(), control.getvalue())

    def test_resumed_vararg_is_still_a_tuple(self):
        vm, _ = self._freeze_inside_call()
        document = encode_graph(vm.state_root())
        restored = decode_graph(json.loads(json.dumps(document)))
        frame = restored["frames"][-1]
        self.assertIsInstance(frame["locals"]["rest"], tuple)
        self.assertIsInstance(frame["locals"]["extra"], dict)


if __name__ == "__main__":
    unittest.main()
