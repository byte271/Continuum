"""VM-owned classes and instances.

The defining constraint is that no host type object or host instance is ever
created: a class is a namespace the runtime interprets, and every live value
stays in the portable graph.
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
from continuum.values import (
    BoundMethodValue,
    ClassValue,
    FunctionValue,
    InstanceValue,
)
from continuum.vm import VirtualMachine

DIFFERENTIAL_CASES = {
    "construct_and_call_a_method": (
        "class C:\n"
        "    def __init__(self, v):\n"
        "        self.v = v\n"
        "    def get(self):\n"
        "        return self.v\n"
        "print(C(3).get())\n"
    ),
    "class_attribute_is_shared": (
        "class C:\n"
        "    tag = 'x'\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "a = C()\n"
        "b = C()\n"
        "print(a.tag, b.tag, C.tag)\n"
    ),
    "instance_attribute_shadows_the_class": (
        "class C:\n"
        "    tag = 'class'\n"
        "    def __init__(self):\n"
        "        self.tag = 'inst'\n"
        "print(C().tag, C.tag)\n"
    ),
    "class_without_init_takes_no_arguments": (
        "class C:\n"
        "    def hi(self):\n"
        "        return 'hi'\n"
        "print(C().hi())\n"
        "try:\n"
        "    C(1)\n"
        "except TypeError:\n"
        "    print('rejected')\n"
    ),
    "method_defaults_bind_normally": (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "    def add(self, k=5):\n"
        "        self.n = self.n + k\n"
        "        return self.n\n"
        "c = C()\n"
        "print(c.add(), c.add(2))\n"
    ),
    "missing_attribute_raises": (
        "class C:\n"
        "    pass\n"
        "try:\n"
        "    C().nope\n"
        "except AttributeError:\n"
        "    print('attribute error')\n"
    ),
    "instances_hold_independent_state": (
        "class C:\n"
        "    def __init__(self, v):\n"
        "        self.v = v\n"
        "a = C(1)\n"
        "b = C(2)\n"
        "a.v = 9\n"
        "print(a.v, b.v)\n"
    ),
    "instances_may_reference_each_other": (
        "class Node:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "        self.peer = None\n"
        "x = Node('x')\n"
        "y = Node('y')\n"
        "x.peer = y\n"
        "y.peer = x\n"
        "print(x.peer.name, y.peer.name, x.peer.peer.name)\n"
    ),
    "method_calls_another_method": (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.n = 1\n"
        "    def double(self):\n"
        "        self.n = self.n * 2\n"
        "        return self.n\n"
        "    def quad(self):\n"
        "        self.double()\n"
        "        return self.double()\n"
        "print(C().quad())\n"
    ),
    "init_must_return_none": (
        "class C:\n"
        "    def __init__(self):\n"
        "        return 5\n"
        "try:\n"
        "    C()\n"
        "except TypeError:\n"
        "    print('init must return None')\n"
    ),
    "method_closes_over_an_enclosing_variable": (
        "def make(scale):\n"
        "    class C:\n"
        "        def apply(self, v):\n"
        "            return v * scale\n"
        "    return C\n"
        "print(make(3)().apply(4))\n"
    ),
    "attribute_assignment_on_a_non_instance_fails": (
        "try:\n"
        "    x = 5\n"
        "    x.attr = 1\n"
        "except AttributeError:\n"
        "    print('cannot set')\n"
    ),
    "methods_accept_star_arguments": (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.seen = []\n"
        "    def take(self, *values, tag='t', **extra):\n"
        "        self.seen.append((values, tag, sorted(extra.items())))\n"
        "        return len(self.seen)\n"
        "c = C()\n"
        "print(c.take(1, 2, tag='x', k=3), c.seen)\n"
    ),
    "exception_raised_inside_a_method_is_catchable": (
        "class C:\n"
        "    def boom(self):\n"
        "        raise ValueError('inside')\n"
        "try:\n"
        "    C().boom()\n"
        "except ValueError as error:\n"
        "    print('caught', str(error))\n"
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


class ClassDifferentialTests(unittest.TestCase):
    def test_class_semantics_match_cpython(self):
        for name, source in DIFFERENTIAL_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(run_continuum(source), run_cpython(source))

    def test_excluded_class_features_are_rejected(self):
        cases = {
            "base class": "class C(dict):\n    pass\n",
            "metaclass": "class C(metaclass=type):\n    pass\n",
            "decorator": (
                "def d(c):\n    return c\n\n@d\nclass C:\n    pass\n"
            ),
            "loop in the class body": (
                "class C:\n    for i in range(2):\n        pass\n"
            ),
            "duplicate member": (
                "class C:\n"
                "    def f(self):\n"
                "        return 1\n"
                "    def f(self):\n"
                "        return 2\n"
            ),
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(CompileError):
                    compile_source(source, "<case>")


class NoHostTypesTests(unittest.TestCase):
    SOURCE = (
        "class Point:\n"
        "    origin = 0\n"
        "    def __init__(self, x):\n"
        "        self.x = x\n"
        "    def shift(self, d):\n"
        "        self.x = self.x + d\n"
        "        return self.x\n"
        "p = Point(1)\n"
        "p.shift(2)\n"
    )

    def _run(self):
        vm = VirtualMachine(
            compile_source(self.SOURCE, "<case>"), ["<case>"], "<case>"
        )
        vm.run()
        return vm

    def test_class_and_instance_are_vm_values_not_host_objects(self):
        vm = self._run()
        cls = vm.globals["Point"]
        instance = vm.globals["p"]
        self.assertIsInstance(cls, ClassValue)
        self.assertIsInstance(instance, InstanceValue)
        # Not a host type and not a host instance of one.
        self.assertNotIsInstance(cls, type)
        self.assertIs(type(instance), InstanceValue)
        self.assertIs(instance.cls, cls)
        self.assertIsInstance(cls.members["shift"], FunctionValue)

    def test_every_live_value_encodes_without_a_host_type(self):
        vm = self._run()
        document = encode_graph(vm.state_root())
        kinds = {node.get("kind") for node in document["objects"]}
        self.assertIn("class", kinds)
        self.assertIn("instance", kinds)
        # A host type would have had no encoding at all; assert the graph is
        # complete by round-tripping it through JSON.
        restored = decode_graph(json.loads(json.dumps(document)))
        self.assertIsInstance(restored["globals"]["Point"], ClassValue)

    def test_bound_method_is_a_portable_value(self):
        vm = self._run()
        instance = vm.globals["p"]
        method = vm._instance_attribute(instance, "shift")
        self.assertIsInstance(method, BoundMethodValue)
        restored = decode_graph(
            json.loads(json.dumps(encode_graph({"m": method})))
        )["m"]
        self.assertIsInstance(restored, BoundMethodValue)
        self.assertIsInstance(restored.instance, InstanceValue)
        self.assertIsInstance(restored.function, FunctionValue)


class ClassCodecTests(unittest.TestCase):
    def test_instances_share_one_class_after_a_round_trip(self):
        cls = ClassValue("m.C@1", "C", {"tag": "t"})
        first = InstanceValue(cls, {"n": 1})
        second = InstanceValue(cls, {"n": 2})
        restored = decode_graph(
            json.loads(
                json.dumps(encode_graph({"a": first, "b": second}))
            )
        )
        self.assertIs(restored["a"].cls, restored["b"].cls)
        restored["a"].cls.members["tag"] = "changed"
        self.assertEqual(restored["b"].cls.members["tag"], "changed")

    def test_mutually_referencing_instances_round_trip(self):
        cls = ClassValue("m.N@1", "N", {})
        left = InstanceValue(cls, {})
        right = InstanceValue(cls, {"peer": left})
        left.attributes["peer"] = right
        restored = decode_graph(
            json.loads(json.dumps(encode_graph({"left": left})))
        )["left"]
        self.assertIs(restored.attributes["peer"].attributes["peer"], restored)

    def test_malformed_class_records_are_rejected(self):
        cases = {
            "members are not a mapping": ("class", "members"),
            "instance class is missing": ("instance", "cls"),
            "instance attributes are missing": ("instance", "attributes"),
        }
        for name, (kind, field_name) in cases.items():
            with self.subTest(case=name):
                document = encode_graph(
                    {"i": InstanceValue(ClassValue("m.C@1", "C", {}), {})}
                )
                for node in document["objects"]:
                    if node.get("kind") == kind:
                        node[field_name] = {"t": "int", "v": "1"}
                with self.assertRaises(ImageError):
                    decode_graph(document)

    def test_instance_attribute_keys_must_be_strings(self):
        instance = InstanceValue(ClassValue("m.C@1", "C", {}), {"ok": 1})
        document = encode_graph({"i": instance})
        # Rewrite exactly the dictionary the instance points at, not any
        # other dictionary in the graph.
        attributes_id = None
        for node in document["objects"]:
            if node.get("kind") == "instance":
                attributes_id = node["attributes"]["id"]
        self.assertIsNotNone(attributes_id)
        document["objects"][attributes_id]["items"] = [
            [{"t": "int", "v": "1"}, {"t": "int", "v": "2"}]
        ]
        with self.assertRaises(ImageError):
            decode_graph(document)


class ClassContinuationTests(unittest.TestCase):
    SOURCE = (
        "class Account:\n"
        "    def __init__(self, owner):\n"
        "        self.owner = owner\n"
        "        self.entries = []\n"
        "    def post(self, amount):\n"
        "        self.entries.append(amount)\n"
        "        return len(self.entries)\n"
        "    def total(self):\n"
        "        running = 0\n"
        "        for entry in self.entries:\n"
        "            running = running + entry\n"
        "        return running\n"
        "\n"
        "account = Account('a')\n"
        "index = 0\n"
        "while index < 8:\n"
        "    account.post(index)\n"
        "    index = index + 1\n"
        "print(account.owner, account.total(), len(account.entries))\n"
    )

    def _freeze_inside_method(self):
        def callback(vm):
            frame = vm.frames[-1]
            # `post`, not `__init__`: the module must already hold the
            # instance so both references can be compared after a restore.
            if "self" in frame.locals and "amount" in frame.locals:
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

    def test_instance_survives_an_image_taken_inside_a_method(self):
        vm, before = self._freeze_inside_method()
        self.assertIsInstance(vm.frames[-1].locals["self"], InstanceValue)

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "class.cont"
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

    def test_resumed_instance_is_the_same_object_the_globals_hold(self):
        vm, _ = self._freeze_inside_method()
        restored = decode_graph(
            json.loads(json.dumps(encode_graph(vm.state_root())))
        )
        frame_self = restored["frames"][-1]["locals"]["self"]
        module_account = restored["globals"]["account"]
        # A copy here would let the method mutate state the module cannot see.
        self.assertIs(frame_self, module_account)

    def test_freeze_inside_a_constructor_resumes(self):
        source = (
            "class Slow:\n"
            "    def __init__(self, n):\n"
            "        self.items = []\n"
            "        index = 0\n"
            "        while index < n:\n"
            "            self.items.append(index)\n"
            "            index = index + 1\n"
            "\n"
            "s = Slow(6)\n"
            "print(len(s.items))\n"
        )

        def callback(vm):
            if any(frame.discard_result for frame in vm.frames):
                raise FrozenExecution

        vm = VirtualMachine(
            compile_source(source, "<case>"),
            ["<case>"],
            "<case>",
            safe_point_callback=callback,
        )
        before = io.StringIO()
        with contextlib.redirect_stdout(before):
            with self.assertRaises(FrozenExecution):
                vm.run()

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "init.cont"
            save_image(str(image), vm, source)
            loaded = load_image(str(image))
            loaded.validate_compatibility()
            resumed = loaded.restore_vm(None, {})
            after = io.StringIO()
            with contextlib.redirect_stdout(after):
                resumed.run()

        self.assertEqual(before.getvalue() + after.getvalue(), "6\n")


if __name__ == "__main__":
    unittest.main()
