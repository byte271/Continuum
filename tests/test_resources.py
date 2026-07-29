from __future__ import annotations

import io
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from continuum.compiler import compile_source
from continuum.errors import FrozenExecution, ResourceError
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine


SOURCE = """
import random

def work(path):
    rng = random.Random(909)
    handle = open(path, "r", encoding="utf-8")
    prefix = handle.read(7)
    first = rng.randint(1, 100000)
    index = 0
    values = []
    while index < 12:
        values.append(rng.randint(1, 100000))
        index += 1
    tail = handle.read()
    print("RESULT", prefix, first, values[-1], tail)
    return values

result = work(__args__[1])
"""


class ResourceTests(unittest.TestCase):
    def test_file_offset_and_random_state_survive_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "input.txt"
            data.write_text("abcdefg-remaining-data", encoding="utf-8")
            image = root / "resource.cont"
            frozen = False

            def checkpoint(vm):
                nonlocal frozen
                frame = vm.frames[-1]
                if frozen or frame.locals.get("index") != 5:
                    return
                save_image(image, vm, SOURCE)
                frozen = True
                raise FrozenExecution

            vm = VirtualMachine(
                compile_source(SOURCE, "resource.py"),
                ["resource.py", str(data)],
                "resource.py",
                resource_policy="bundle",
                safe_point_callback=checkpoint,
            )
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(FrozenExecution):
                    vm.run()
            for resource in vm.resources.files.values():
                resource.close()
            data.unlink()
            resumed = load_image(image).restore_vm("bundle")
            handle = resumed.frames[-1].locals["handle"]
            self.assertEqual(handle.tell(), 7)
            output = io.StringIO()
            with redirect_stdout(output):
                resumed.run()
            self.assertIn("abcdefg", output.getvalue())
            self.assertIn("-remaining-data", output.getvalue())

    def test_strict_policy_rejects_changed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "input.txt"
            data.write_text("original", encoding="utf-8")
            image = root / "resource.cont"
            vm = VirtualMachine(
                compile_source(SOURCE, "resource.py"),
                ["resource.py", str(data)],
                "resource.py",
                resource_policy="strict",
            )
            while "handle" not in vm.frames[-1].locals:
                vm.step()
            save_image(image, vm, SOURCE)
            for resource in vm.resources.files.values():
                resource.close()
            data.write_text("modified", encoding="utf-8")
            with self.assertRaises(ResourceError):
                load_image(image).restore_vm("strict")

    def test_relocate_rebinds_matching_content_at_a_new_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "linux-path.txt"
            relocated = root / "macos-path.txt"
            original.write_text("abcdefg-remaining-data", encoding="utf-8")
            image = root / "relocate.cont"
            vm = VirtualMachine(
                compile_source(SOURCE, "resource.py"),
                ["resource.py", str(original)],
                "resource.py",
                resource_policy="strict",
            )
            while vm.frames[-1].locals.get("prefix") != "abcdefg":
                vm.step()
            save_image(image, vm, SOURCE)
            for resource in vm.resources.files.values():
                resource.close()
            relocated.write_bytes(original.read_bytes())
            original.unlink()
            os.utime(relocated, None)
            resumed = load_image(image).restore_vm(
                "relocate",
                {str(original.resolve()): str(relocated.resolve())},
            )
            self.assertEqual(resumed.frames[-1].locals["handle"].tell(), 7)
            with redirect_stdout(io.StringIO()):
                resumed.run()
            self.assertEqual(len(resumed.globals["result"]), 12)
            for resource in resumed.resources.files.values():
                resource.close()

    def test_multiple_bundled_file_offsets_survive(self):
        source = """
def work(left_path, right_path):
    left = open(left_path, "r", encoding="utf-8")
    right = open(right_path, "r", encoding="utf-8")
    left_prefix = left.read(2)
    right_prefix = right.read(3)
    index = 0
    while index < 8:
        index += 1
    result = left_prefix + left.read() + ":" + right_prefix + right.read()
    return result

answer = work(__args__[1], __args__[2])
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left.txt"
            right = root / "right.txt"
            left.write_text("LEFT", encoding="utf-8")
            right.write_text("RIGHT", encoding="utf-8")
            image = root / "multiple.cont"
            vm = VirtualMachine(
                compile_source(source, "multiple_resources.py"),
                ["multiple_resources.py", str(left), str(right)],
                "multiple_resources.py",
                resource_policy="bundle",
            )
            while (
                len(vm.frames) < 2
                or vm.frames[-1].locals.get("index") != 3
            ):
                vm.step()
            save_image(image, vm, source)
            for resource in vm.resources.files.values():
                resource.close()
            left.unlink()
            right.unlink()
            resumed = load_image(image).restore_vm("bundle")
            self.assertEqual(resumed.frames[-1].locals["left"].tell(), 2)
            self.assertEqual(resumed.frames[-1].locals["right"].tell(), 3)
            resumed.run()
            self.assertEqual(resumed.globals["answer"], "LEFT:RIGHT")
            for resource in resumed.resources.files.values():
                resource.close()


if __name__ == "__main__":
    unittest.main()
