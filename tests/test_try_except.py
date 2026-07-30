"""Portable try/except: semantics, portability, and continuation.

Every case here is checked against CPython running the identical source, so a
divergence is a failure rather than a documented difference.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from continuum.codec import decode_graph, encode_graph
from continuum.compiler import compile_source
from continuum.errors import CompileError, FrozenExecution, ImageError
from continuum.image import load_image, save_image
from continuum.vm import VirtualMachine

# (name, source) pairs whose observable behavior must equal CPython's.
DIFFERENTIAL_CASES = {
    "handler_matches": (
        "try:\n"
        "    x = 1 // 0\n"
        "except ZeroDivisionError:\n"
        "    print('caught')\n"
        "print('after')\n"
    ),
    "first_matching_handler_wins": (
        "try:\n"
        "    raise KeyError('k')\n"
        "except IndexError:\n"
        "    print('wrong')\n"
        "except (KeyError, TypeError):\n"
        "    print('tuple')\n"
        "except KeyError:\n"
        "    print('later handler must not run')\n"
    ),
    "base_class_matches_subclass": (
        "try:\n"
        "    raise IndexError('i')\n"
        "except LookupError as error:\n"
        "    print('base', str(error))\n"
    ),
    "handler_name_is_unbound_afterwards": (
        "try:\n"
        "    raise ValueError('boom')\n"
        "except ValueError as error:\n"
        "    print('caught', str(error))\n"
        "try:\n"
        "    print(error)\n"
        "except NameError:\n"
        "    print('unbound')\n"
    ),
    "else_runs_only_without_exception": (
        "try:\n"
        "    value = 5\n"
        "except ValueError:\n"
        "    print('no')\n"
        "else:\n"
        "    print('else', value)\n"
        "finally:\n"
        "    print('finally')\n"
    ),
    "finally_runs_before_outer_handler": (
        "try:\n"
        "    try:\n"
        "        raise TypeError('inner')\n"
        "    except ValueError:\n"
        "        print('wrong')\n"
        "    finally:\n"
        "        print('inner finally')\n"
        "except TypeError as error:\n"
        "    print('outer', str(error))\n"
    ),
    "bare_except_catches": (
        "try:\n"
        "    raise RuntimeError('r')\n"
        "except:\n"
        "    print('bare')\n"
    ),
    "raise_from_handler_reaches_outer": (
        "try:\n"
        "    try:\n"
        "        raise ValueError('v')\n"
        "    except ValueError:\n"
        "        raise KeyError('second')\n"
        "except KeyError as error:\n"
        "    print('outer', str(error))\n"
    ),
    "return_out_of_try_except": (
        "def divide(n):\n"
        "    try:\n"
        "        return 10 // n\n"
        "    except ZeroDivisionError:\n"
        "        return -1\n"
        "\n"
        "print(divide(2), divide(0))\n"
    ),
    "handler_inside_loop_keeps_state": (
        "total = 0\n"
        "seen = []\n"
        "for i in range(5):\n"
        "    try:\n"
        "        if i % 2 == 0:\n"
        "            raise ValueError(str(i))\n"
        "        total = total + i\n"
        "    except ValueError as error:\n"
        "        seen.append(str(error))\n"
        "        total = total + 100\n"
        "print(total, seen)\n"
    ),
    "unmatched_handler_leaves_finally_intact": (
        "try:\n"
        "    try:\n"
        "        raise KeyError('k')\n"
        "    except ValueError:\n"
        "        print('wrong')\n"
        "except KeyError:\n"
        "    print('propagated')\n"
    ),
    "nested_handlers_in_function": (
        "def classify(n):\n"
        "    try:\n"
        "        if n < 0:\n"
        "            raise ValueError('negative')\n"
        "        return 100 // n\n"
        "    except ValueError:\n"
        "        return -1\n"
        "    except ZeroDivisionError:\n"
        "        return -2\n"
        "\n"
        "print(classify(4), classify(-1), classify(0))\n"
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
        # An exception nothing handled is reported by the runtime rather than
        # raised through the host, so compare the underlying type.
        cause = exc.__cause__ or exc
        return type(cause).__name__, output.getvalue()
    return "ok", output.getvalue()


class TryExceptDifferentialTests(unittest.TestCase):
    def test_every_case_matches_cpython(self):
        for name, source in DIFFERENTIAL_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(run_continuum(source), run_cpython(source))

    def test_unhandled_exception_still_reports_the_original_type(self):
        source = "try:\n    raise ValueError('x')\nexcept KeyError:\n    pass\n"
        status, printed = run_continuum(source)
        self.assertEqual(status, "ValueError")
        self.assertEqual(printed, "")

    def test_catching_a_non_exception_is_rejected(self):
        for name, source in {
            "literal": "try:\n    raise ValueError('x')\nexcept 5:\n    pass\n",
            "non-exception builtin": (
                "try:\n    raise ValueError('x')\nexcept len:\n    pass\n"
            ),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(run_continuum(source)[0], "TypeError")

    def test_exception_type_outside_the_allowlist_is_not_nameable(self):
        # SystemExit and KeyboardInterrupt are deliberately absent from the
        # allowlist, so a program cannot name one, let alone catch it.
        for name in ("SystemExit", "KeyboardInterrupt"):
            with self.subTest(case=name):
                source = (
                    "try:\n"
                    "    raise ValueError('x')\n"
                    f"except {name}:\n"
                    "    pass\n"
                )
                self.assertEqual(run_continuum(source)[0], "NameError")

    def test_a_freeze_is_never_swallowed_by_a_handler(self):
        # A bare `except:` must not be able to capture a checkpoint request.
        source = (
            "index = 0\n"
            "while index < 50:\n"
            "    try:\n"
            "        index = index + 1\n"
            "    except:\n"
            "        print('swallowed the freeze')\n"
        )

        def callback(vm):
            if vm.safe_points_executed > 3:
                raise FrozenExecution

        vm = VirtualMachine(
            compile_source(source, "<case>"),
            ["<case>"],
            "<case>",
            safe_point_callback=callback,
        )
        with self.assertRaises(FrozenExecution):
            vm.run()

    def test_control_transfer_out_of_a_protected_region_is_rejected(self):
        for name, source in {
            "break": (
                "for i in range(2):\n"
                "    try:\n"
                "        break\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
            "continue": (
                "for i in range(2):\n"
                "    try:\n"
                "        continue\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
            "return in finally": (
                "def f():\n"
                "    try:\n"
                "        return 1\n"
                "    finally:\n"
                "        pass\n"
            ),
        }.items():
            with self.subTest(case=name):
                with self.assertRaises(CompileError):
                    compile_source(source, "<case>")


class ExceptionGraphCodecTests(unittest.TestCase):
    def test_live_exception_round_trips_with_identity_preserved(self):
        error = ValueError("payload")
        shared = [error, error]
        root = {"shared": shared, "single": error}
        document = encode_graph(root)
        restored = decode_graph(json.loads(json.dumps(document)))

        self.assertIsInstance(restored["single"], ValueError)
        self.assertEqual(restored["single"].args, ("payload",))
        self.assertIs(restored["shared"][0], restored["shared"][1])
        self.assertIs(restored["shared"][0], restored["single"])

    def test_exception_inside_frame_state_round_trips(self):
        source = (
            "try:\n"
            "    raise KeyError('frozen')\n"
            "except KeyError as error:\n"
            "    marker = str(error)\n"
        )
        vm = VirtualMachine(compile_source(source, "<case>"), ["<c>"], "<c>")
        vm.run()
        document = encode_graph(vm.state_root())
        restored = decode_graph(json.loads(json.dumps(document)))
        self.assertEqual(restored["globals"]["marker"], "'frozen'")

    def test_unknown_exception_record_is_rejected(self):
        document = encode_graph({"error": ValueError("x")})
        for node in document["objects"]:
            if node.get("kind") == "exception":
                node["name"] = "NotARealException"
        with self.assertRaises(ImageError):
            decode_graph(document)

    def test_non_exception_builtin_record_is_rejected(self):
        document = encode_graph({"error": ValueError("x")})
        for node in document["objects"]:
            if node.get("kind") == "exception":
                node["name"] = "len"
        with self.assertRaises(ImageError):
            decode_graph(document)


class TryExceptContinuationTests(unittest.TestCase):
    """Freeze while a handler frame is live, then resume it elsewhere."""

    SOURCE = (
        "log = []\n"
        "def work(total):\n"
        "    index = 0\n"
        "    while index < total:\n"
        "        try:\n"
        "            if index % 3 == 0:\n"
        "                raise ValueError(str(index))\n"
        "            log.append(index)\n"
        "        except ValueError as error:\n"
        "            log.append(str(error))\n"
        "        index = index + 1\n"
        "    return len(log)\n"
        "\n"
        "print(work(9))\n"
        "print(log)\n"
    )

    def _freeze_at(self, predicate):
        """Run until `predicate` holds at a safe point, then snapshot."""

        captured: dict[str, object] = {}

        def callback(vm):
            if "state" not in captured and predicate(vm):
                captured["state"] = vm.state_root()
                captured["frames"] = len(vm.frames)
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
        return vm, captured, output.getvalue()

    def test_freeze_inside_an_active_handler_and_resume(self):
        def inside_handler(vm):
            frame = vm.frames[-1]
            return any(
                isinstance(value, BaseException) for value in frame.stack
            ) or "error" in frame.locals

        vm, captured, _ = self._freeze_at(inside_handler)
        self.assertIn("state", captured)
        self.assertGreaterEqual(captured["frames"], 2)

        document = encode_graph(captured["state"])
        restored_state = decode_graph(json.loads(json.dumps(document)))
        self.assertEqual(
            [frame["function_id"] for frame in restored_state["frames"]],
            [frame.function_id for frame in vm.frames],
        )

    def test_image_written_with_a_live_except_block_resumes_identically(self):
        def inside_try(vm):
            return any(
                block["kind"] == "except"
                for frame in vm.frames
                for block in frame.blocks
            )

        vm, captured, before = self._freeze_at(inside_try)
        blocks = [
            block
            for frame in vm.frames
            for block in frame.blocks
            if block["kind"] == "except"
        ]
        self.assertTrue(blocks, "expected a live except block")

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "handler.cont"
            save_image(str(image), vm, self.SOURCE)
            loaded = load_image(str(image))
            loaded.validate_compatibility()
            resumed = loaded.restore_vm(None, {})

            after = io.StringIO()
            with contextlib.redirect_stdout(after):
                resumed.run()

        control = io.StringIO()
        control_vm = VirtualMachine(
            compile_source(self.SOURCE, "<case>"), ["<case>"], "<case>"
        )
        with contextlib.redirect_stdout(control):
            control_vm.run()

        self.assertEqual(before + after.getvalue(), control.getvalue())

    def test_image_with_an_unknown_control_block_is_rejected(self):
        def inside_try(vm):
            return any(
                block["kind"] == "except"
                for frame in vm.frames
                for block in frame.blocks
            )

        vm, _, _ = self._freeze_at(inside_try)
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "tampered.cont"
            save_image(str(image), vm, self.SOURCE)
            tampered = Path(temporary) / "rewritten.cont"
            _rewrite_heap(image, tampered, "except", "teleport")

            loaded = load_image(str(tampered))
            loaded.validate_compatibility()
            with self.assertRaises(Exception):
                loaded.restore_vm(None, {}).run()


def _rewrite_heap(source: Path, target: Path, old: str, new: str) -> None:
    """Copy an image, replacing a control-block kind, and refresh checksums."""

    import hashlib

    with zipfile.ZipFile(source, "r") as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    heap = payload["heap/objects.json"].decode("utf-8")
    payload["heap/objects.json"] = heap.replace(
        f'"{old}"', f'"{new}"'
    ).encode("utf-8")
    checksums = json.loads(payload["checksums.json"])
    for name in checksums["entries"]:
        checksums["entries"][name] = hashlib.sha256(payload[name]).hexdigest()
    payload["checksums.json"] = (
        json.dumps(checksums, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)


if __name__ == "__main__":
    unittest.main()
