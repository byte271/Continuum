"""Sensitivity of the cross-Python differential comparison.

A differential suite reporting "zero mismatches" proves nothing unless the
comparison can actually detect a mismatch. These tests corrupt each dimension
the suite claims to compare and assert that the corruption is caught, so a
green corpus run is evidence about Continuum rather than evidence that the
comparison is blind.

They run on a single interpreter: the comparison operates on fingerprints, so
its sensitivity is testable without a second Python.
"""

from __future__ import annotations

import copy
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from validation.cross_python.differential import (  # noqa: E402
    ACCEPTED,
    MISMATCH,
    Fingerprinter,
    compare,
    fingerprint,
    run_control,
    safe_points_for,
    source_case,
    target_case,
)

PROGRAM = """
def make_counter(start):
    def bump(step):
        return step + start
    return bump


def leaf(limit, bag, bump, graph):
    index = 0
    while index < limit:
        bag.append(bump(index))
        graph["shared"].append(index)
        print(f"ACTION {index}")
        index += 1
    return len(bag)


def middle(limit, bag, bump, graph):
    return leaf(limit, bag, bump, graph)


def outer(limit):
    bag = []
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    bump = make_counter(5)
    total = middle(limit, bag, bump, graph)
    print(f"FINAL {total}")
    return total


answer = outer(30)
"""


class DifferentialFixture(unittest.TestCase):
    """One real cross-safepoint case, reused by every sensitivity test."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        image = Path(cls.temporary.name) / "case.cont"
        cls.control = run_control(PROGRAM, "p.py")
        # A checkpoint deep enough to have a real frame chain and live cells.
        cls.source = source_case(PROGRAM, "p.py", 40, image)
        assert cls.source["status"] == "frozen", cls.source
        cls.target = target_case(image)
        assert cls.target["status"] == "restored", cls.target

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def parts(self):
        return (
            copy.deepcopy(self.source),
            copy.deepcopy(self.target),
            copy.deepcopy(self.control),
        )

    def assertDetected(self, mutate, expected_substring):
        source, target, control = self.parts()
        mutate(source, target, control)
        differences = compare(source, target, control)
        self.assertTrue(
            differences, f"corruption was not detected: {expected_substring}"
        )
        self.assertTrue(
            any(expected_substring in item for item in differences),
            f"expected {expected_substring!r} in {differences}",
        )


class BaselineTests(DifferentialFixture):
    def test_the_unmodified_case_compares_clean(self):
        source, target, control = self.parts()
        self.assertEqual(compare(source, target, control), [])

    def test_the_case_really_captured_a_live_frame_chain(self):
        chain = self.source["fingerprint"]["frame_chain"]
        self.assertEqual(chain, ["__module__", "outer", "middle", "leaf"])

    def test_verification_ran_before_restore_and_accepted_the_contract(self):
        self.assertEqual(self.target["verification"]["integrity"], "verified")
        self.assertEqual(self.target["verification"]["compatibility"], "accepted")
        self.assertEqual(self.target["verification"]["policy"], "execution-abi")


class SensitivityTests(DifferentialFixture):
    def test_detects_a_changed_frame_chain(self):
        self.assertDetected(
            lambda s, t, c: t["fingerprint"]["frame_chain"].append("ghost"),
            "frame chain",
        )

    def test_detects_a_dropped_frame(self):
        self.assertDetected(
            lambda s, t, c: t["fingerprint"]["frames"].pop(),
            "frame count changed",
        )

    def test_detects_a_moved_resume_position(self):
        def mutate(source, target, control):
            target["fingerprint"]["frames"][-1]["resume_pc"] += 1

        self.assertDetected(mutate, "resume position changed")

    def test_detects_a_changed_resume_opcode(self):
        def mutate(source, target, control):
            target["fingerprint"]["frames"][-1]["resume_op"] = "NOPE"

        self.assertDetected(mutate, "resume opcode changed")

    def test_detects_changed_locals(self):
        def mutate(source, target, control):
            target["fingerprint"]["frames"][-1]["locals"][0][1] = {"t": "int", "v": -1}

        self.assertDetected(mutate, "locals changed")

    def test_the_closure_cell_is_part_of_the_compared_structure(self):
        """`make_counter` has returned, so its cell hangs off the function value.

        The cell is still compared -- it is reachable from the live `bump`
        binding -- so corrupting it is detected as a change to that frame's
        locals rather than going unnoticed.
        """

        rendered = json.dumps(self.source["fingerprint"])
        self.assertIn('"cell"', rendered)

        def mutate(source, target, control):
            for frame in target["fingerprint"]["frames"]:
                for name, value in frame["locals"]:
                    if name == "bump":
                        value["closure"] = [
                            {"t": "cell", "id": "tampered", "value": {"t": "int", "v": 0}}
                        ]
                        return
            raise AssertionError("fixture lost its closure binding")

        self.assertDetected(mutate, "locals changed")

    def test_detects_a_changed_operand_stack(self):
        def mutate(source, target, control):
            frames = target["fingerprint"]["frames"]
            for frame in frames:
                if frame["operand_stack"]:
                    frame["operand_stack"].append({"t": "int", "v": 99})
                    return
            # Every checkpoint here has at least one non-empty operand stack in
            # some frame; if not, force the dimension to differ anyway.
            frames[-1]["operand_stack"].append({"t": "int", "v": 99})

        self.assertDetected(mutate, "operand stack changed")

    def test_detects_changed_control_blocks(self):
        def mutate(source, target, control):
            target["fingerprint"]["frames"][-1]["control_blocks"].append(
                {"kind": "invented"}
            )

        self.assertDetected(mutate, "control blocks changed")

    def test_detects_changed_pending_finally_state(self):
        def mutate(source, target, control):
            target["fingerprint"]["frames"][-1]["finally_reasons"].append(
                {"kind": "invented"}
            )

        self.assertDetected(mutate, "pending finally state changed")

    def test_detects_changed_module_rng_state(self):
        def mutate(source, target, control):
            target["fingerprint"]["module_random_state"] = {"t": "tuple", "items": []}

        self.assertDetected(mutate, "module RNG state changed")

    def test_detects_changed_globals(self):
        def mutate(source, target, control):
            target["fingerprint"]["globals"].append(["injected", {"t": "int", "v": 1}])

        self.assertDetected(mutate, "module globals changed")

    def test_detects_a_changed_instruction_counter(self):
        def mutate(source, target, control):
            target["fingerprint"]["instructions_executed"] += 1

        self.assertDetected(mutate, "instruction counter changed")

    def test_detects_a_changed_safe_point_counter(self):
        def mutate(source, target, control):
            target["fingerprint"]["safe_points_executed"] += 1

        self.assertDetected(mutate, "safe-point counter changed")

    def test_detects_replayed_completed_work(self):
        """The anti-replay control: re-emitting a completed action is caught."""

        def mutate(source, target, control):
            first_action = next(
                line
                for line in source["stdout"].splitlines()
                if line.startswith("ACTION")
            )
            target["stdout"] = first_action + "\n" + target["stdout"]

        self.assertDetected(mutate, "completed actions repeated")

    def test_detects_a_restart_from_program_entry(self):
        """A target that reran the whole program is caught, not accepted."""

        def mutate(source, target, control):
            target["stdout"] = control["stdout"]

        differences = self.detect_all(mutate)
        self.assertTrue(differences)

    def test_detects_a_wrong_final_result(self):
        def mutate(source, target, control):
            target["result"] = "'not the answer'"

        self.assertDetected(mutate, "final result differed")

    def test_detects_a_truncated_suffix(self):
        def mutate(source, target, control):
            target["stdout"] = target["stdout"].split("\n", 1)[1]

        self.assertDetected(mutate, "did not match the control")

    def test_detects_changed_stderr(self):
        def mutate(source, target, control):
            target["stderr"] = target["stderr"] + "unexpected diagnostic\n"

        self.assertDetected(mutate, "stderr did not match the control")

    def detect_all(self, mutate):
        source, target, control = self.parts()
        mutate(source, target, control)
        return compare(source, target, control)


LIVE_CELL_PROGRAM = """
def driver(limit):
    tally = 0

    def bump(value):
        nonlocal tally
        tally = tally + value
        return tally

    index = 0
    while index < limit:
        print(f"STEP {index} {bump(index)}")
        index += 1
    return tally


total = driver(30)
print(f"TOTAL {total}")
"""


class LiveEnclosingCellTests(unittest.TestCase):
    """A cell held by a frame that is still on the stack is also compared."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        image = Path(cls.temporary.name) / "live-cell.cont"
        cls.control = run_control(LIVE_CELL_PROGRAM, "live.py")
        cls.source = source_case(LIVE_CELL_PROGRAM, "live.py", 40, image)
        assert cls.source["status"] == "frozen", cls.source
        cls.target = target_case(image)
        assert cls.target["status"] == "restored", cls.target

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_a_live_frame_actually_holds_the_cell(self):
        holders = [
            frame["function_name"]
            for frame in self.source["fingerprint"]["frames"]
            if frame["cells"]
        ]
        self.assertIn("driver", holders)

    def test_the_case_compares_clean_across_the_crossing(self):
        self.assertEqual(
            compare(self.source, self.target, self.control), []
        )

    def test_nonlocal_mutation_through_the_cell_survives(self):
        """The resumed run must keep accumulating into the same binding."""
        combined = self.source["stdout"] + self.target["stdout"]
        self.assertEqual(combined, self.control["stdout"])
        self.assertIn("TOTAL 435", combined)

    def test_corrupting_the_live_cell_is_detected(self):
        source = copy.deepcopy(self.source)
        target = copy.deepcopy(self.target)
        for frame in target["fingerprint"]["frames"]:
            if frame["cells"]:
                frame["cells"][0][1] = {
                    "t": "cell",
                    "id": "tampered",
                    "value": {"t": "int", "v": -1},
                }
                break
        differences = compare(source, target, copy.deepcopy(self.control))
        self.assertTrue(
            any("lexical cells changed" in item for item in differences),
            differences,
        )


class IdentityFingerprintTests(unittest.TestCase):
    """Sharing and cycles must be structural, not merely equal-valued."""

    def test_shared_reference_differs_from_two_equal_copies(self):
        shared: list[int] = [1, 2]
        together = {"left": shared, "right": shared}
        apart = {"left": [1, 2], "right": [1, 2]}
        self.assertNotEqual(
            Fingerprinter().walk(together), Fingerprinter().walk(apart)
        )

    def test_a_shared_reference_emits_a_back_reference(self):
        shared: list[int] = [7]
        encoded = Fingerprinter().walk({"a": shared, "b": shared})
        rendered = json.dumps(encoded)
        self.assertIn('"ref"', rendered)

    def test_a_reference_cycle_terminates_and_is_recorded(self):
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        encoded = Fingerprinter().walk(cycle)
        self.assertIn('"ref"', json.dumps(encoded))

    def test_distinct_random_states_differ(self):
        first = random.Random(1)
        second = random.Random(2)
        self.assertNotEqual(
            Fingerprinter().walk(first), Fingerprinter().walk(second)
        )

    def test_equal_random_states_match(self):
        self.assertEqual(
            Fingerprinter().walk(random.Random(5)),
            Fingerprinter().walk(random.Random(5)),
        )

    def test_int_and_float_and_bool_are_distinguished(self):
        walker = Fingerprinter()
        self.assertNotEqual(walker.walk(1), walker.walk(1.0))
        self.assertNotEqual(walker.walk(1), walker.walk(True))

    def test_dict_insertion_order_is_compared(self):
        self.assertNotEqual(
            Fingerprinter().walk({"a": 1, "b": 2}),
            Fingerprinter().walk({"b": 2, "a": 1}),
        )

    def test_bytes_survive_as_hex(self):
        self.assertEqual(
            Fingerprinter().walk(b"\x00\xff"), {"t": "bytes", "v": "00ff"}
        )


class SafePointSamplingTests(unittest.TestCase):
    def test_sampling_stays_inside_the_run(self):
        for total in (2, 5, 37, 1000):
            with self.subTest(total=total):
                points = safe_points_for(total, 6)
                self.assertTrue(all(1 <= point < total for point in points))

    def test_sampling_is_deterministic(self):
        self.assertEqual(safe_points_for(500, 6), safe_points_for(500, 6))

    def test_a_run_with_no_interior_safe_point_yields_nothing(self):
        self.assertEqual(safe_points_for(1, 6), [])
        self.assertEqual(safe_points_for(0, 6), [])

    def test_short_runs_use_every_interior_safe_point(self):
        self.assertEqual(safe_points_for(4, 10), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
