from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from continuum.compiler import compile_source
from continuum.errors import FrozenExecution
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine


class DemonstrationTests(unittest.TestCase):
    def test_demo_final_hash_matches_control_after_midrun_restore(self):
        repository = Path(__file__).resolve().parents[1]
        program_path = repository / "examples" / "demo.py"
        source = program_path.read_text(encoding="utf-8")
        ir = compile_source(source, str(program_path))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.txt"
            input_path.write_text(
                (repository / "examples" / "demo_input.txt").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            argv = [str(program_path), str(input_path), "120"]

            control = VirtualMachine(
                ir, argv, str(program_path), resource_policy="bundle"
            )
            control_output = io.StringIO()
            with redirect_stdout(control_output):
                control.run()

            image = root / "demo.cont"
            did_freeze = False

            def checkpoint(vm):
                nonlocal did_freeze
                frame = vm.frames[-1]
                name = vm.ir["functions"][frame.function_id]["name"]
                if did_freeze or name != "inner_step" or frame.locals.get("index") != 63:
                    return
                save_image(image, vm, source)
                did_freeze = True
                raise FrozenExecution

            source_vm = VirtualMachine(
                ir,
                argv,
                str(program_path),
                resource_policy="bundle",
                safe_point_callback=checkpoint,
            )
            before = io.StringIO()
            with redirect_stdout(before):
                with self.assertRaises(FrozenExecution):
                    source_vm.run()
            self.assertEqual(len(source_vm.frames), 4)
            for resource in source_vm.resources.files.values():
                resource.close()
            input_path.unlink()

            target_vm = load_image(image).restore_vm("bundle")
            after = io.StringIO()
            with redirect_stdout(after):
                target_vm.run()
            combined = before.getvalue() + after.getvalue()
            self.assertEqual(combined, control_output.getvalue())
            self.assertEqual(combined.count("FINAL "), 1)
            self.assertIn("IDENTITY True True", combined)


if __name__ == "__main__":
    unittest.main()

