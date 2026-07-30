from __future__ import annotations

import unittest

from continuum.compiler import compile_source
from continuum.errors import CompileError, ExecutionError
from continuum.vm import VirtualMachine


def run(source: str) -> VirtualMachine:
    vm = VirtualMachine(
        compile_source(source, "language_support.py"),
        ["language_support.py"],
        "language_support.py",
    )
    vm.run()
    return vm


class LanguageSupportTests(unittest.TestCase):
    def test_supported_control_flow_functions_and_containers_in_combination(self):
        source = """
def even(value):
    if value == 0:
        return True
    return odd(value - 1)

def odd(value):
    if value == 0:
        return False
    return even(value - 1)

def add(left, right):
    return left + right

def wrapper(value):
    def increment(item):
        return item + 1
    return increment(value)

events = []
index = 0
while index < 4:
    index += 1
    if index == 2:
        continue
    events.append(index)

total = 0
for left, right in [(1, 2), (3, 4)]:
    total += left + right

mapping = {"events": events, "total": total}
alias = mapping["events"]
functions = [even, odd]
answer = [
    functions[0](10),
    functions[1](9),
    add(right=2, left=3),
    wrapper(4),
    mapping["total"],
    alias is events,
    (True and 7) or 9,
]
"""
        vm = run(source)
        self.assertEqual(vm.globals["answer"], [True, True, 5, 5, 10, True, 7])

    def test_dictionary_key_mutation_during_iteration_fails_explicitly(self):
        source = """
mapping = {"a": 1, "b": 2}
for key in mapping:
    mapping["c"] = 3
"""
        with self.assertRaisesRegex(ExecutionError, "dictionary keys changed"):
            run(source)

    def test_unsupported_constructs_are_rejected_during_compilation(self):
        cases = {
            "duplicate parameter name": "def f(a, *, a):\n    return a\n",
            "global declaration": "def f():\n    global value\n    value = 1\n",
            "nonlocal declaration": (
                "def outer():\n"
                "    value = 1\n"
                "    def inner():\n"
                "        nonlocal value\n"
                "        value = 2\n"
            ),
            "closure capture": (
                "def outer():\n"
                "    value = 1\n"
                "    def inner():\n"
                "        return value\n"
            ),
            "class definition": "class Value:\n    pass\n",
            "list comprehension": "values = [x for x in range(3)]\n",
            "nested comprehension": (
                "values = [(x, y) for x in range(2) for y in range(2)]\n"
            ),
            "generator expression": "values = tuple(x for x in range(3))\n",
            "generator function": "def values():\n    yield 1\n",
            "context manager": "with open('x') as handle:\n    value = handle.read()\n",
            "break out of try": (
                "for i in range(3):\n"
                "    try:\n"
                "        break\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
            "return out of try/finally": (
                "def f():\n"
                "    try:\n"
                "        return 1\n"
                "    finally:\n"
                "        pass\n"
            ),
            "except after bare except": (
                "try:\n"
                "    pass\n"
                "except:\n"
                "    pass\n"
                "except ValueError:\n"
                "    pass\n"
            ),
            "annotated assignment": "value: int = 1\n",
            "attribute assignment": (
                "import random\n"
                "rng = random.Random(1)\n"
                "rng.extra = 2\n"
            ),
            "f-string conversion": "value = f'{1!r}'\n",
            "chained comparison": "value = 1 < 2 < 3\n",
        }
        for name, source in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(CompileError):
                    compile_source(source, f"{name}.py")


if __name__ == "__main__":
    unittest.main()
