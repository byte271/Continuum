"""Negative controls for the anti-replay proof.

The proof now uses a deterministic safe-point hold to decide *when* the
checkpoint is taken. That raises a fair objection: if synchronization can
choose the moment, can it also manufacture the conclusion?

These tests answer it without reintroducing a race. They apply the same
assertions the real proof uses to fabricated audit logs representing each way
a replay could occur, and require every one to be rejected. The assertions are
therefore shown to depend on the observed action record, not on the hold.

`test_adversarial_restart` supplies the positive case against a real migration.
This module supplies the negative space around it.
"""

from __future__ import annotations

import unittest

NONCE = "n0nce"


def entry_count(lines: list[str]) -> int:
    return lines.count(f"ACTION {NONCE} ENTRY")


def prologue_count(lines: list[str], name: str) -> int:
    return lines.count(f"ACTION {NONCE} {name}")


def iteration_actions(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(f"ACTION {NONCE} ITER_")]


def assert_no_replay(case: unittest.TestCase, lines: list[str]) -> None:
    """The proof's own conditions, applied to an audit log.

    Mirrors the assertions in test_adversarial_restart so a fabricated log is
    judged by the same rules a real migration is.
    """

    case.assertEqual(entry_count(lines), 1, "entry module ran more than once")
    for name in ("PROLOGUE_ONE", "PROLOGUE_TWO", "PROLOGUE_WORKER"):
        case.assertEqual(
            prologue_count(lines, name), 1, f"{name} ran more than once"
        )
    actions = iteration_actions(lines)
    case.assertGreaterEqual(len(actions), 11, "too little work was recorded")
    case.assertEqual(
        len(actions), len(set(actions)), "a completed action was repeated"
    )
    indices = [int(line.rsplit("_", 1)[1]) for line in actions]
    case.assertEqual(indices, sorted(indices), "actions are out of order")
    case.assertEqual(
        indices,
        list(range(indices[0], indices[0] + len(indices))),
        "an action is missing",
    )


def clean_log(count: int = 20) -> list[str]:
    """An audit log from a migration in which nothing was replayed."""

    lines = [
        f"ACTION {NONCE} ENTRY",
        f"ACTION {NONCE} PROLOGUE_ONE",
        f"ACTION {NONCE} PROLOGUE_TWO",
        f"ACTION {NONCE} PROLOGUE_WORKER",
    ]
    lines.extend(f"ACTION {NONCE} ITER_{index}" for index in range(count))
    return lines


class PositiveControlTests(unittest.TestCase):
    def test_a_clean_migration_passes(self):
        # If this failed, every negative result below would be meaningless.
        assert_no_replay(self, clean_log())


class NegativeControlTests(unittest.TestCase):
    """Each case is a way replay could happen. All must be rejected."""

    def test_duplicated_completed_action_is_rejected(self):
        lines = clean_log()
        lines.append(f"ACTION {NONCE} ITER_5")
        with self.assertRaises(AssertionError):
            assert_no_replay(self, lines)

    def test_target_repeating_the_entry_marker_is_rejected(self):
        lines = clean_log()
        lines.append(f"ACTION {NONCE} ENTRY")
        with self.assertRaises(AssertionError):
            assert_no_replay(self, lines)

    def test_target_repeating_a_function_prologue_is_rejected(self):
        for name in ("PROLOGUE_ONE", "PROLOGUE_TWO", "PROLOGUE_WORKER"):
            with self.subTest(prologue=name):
                lines = clean_log()
                lines.append(f"ACTION {NONCE} {name}")
                with self.assertRaises(AssertionError):
                    assert_no_replay(self, lines)

    def test_target_restarting_from_iteration_zero_is_rejected(self):
        # The shape a genuine restart would produce: the whole prefix again.
        lines = clean_log(12)
        lines.extend(clean_log(12))
        with self.assertRaises(AssertionError):
            assert_no_replay(self, lines)

    def test_missing_action_is_rejected(self):
        lines = clean_log()
        lines.remove(f"ACTION {NONCE} ITER_7")
        with self.assertRaises(AssertionError):
            assert_no_replay(self, lines)

    def test_out_of_order_actions_are_rejected(self):
        lines = clean_log()
        actions = iteration_actions(lines)
        first = lines.index(actions[0])
        lines[first], lines[first + 3] = lines[first + 3], lines[first]
        with self.assertRaises(AssertionError):
            assert_no_replay(self, lines)

    def test_too_little_work_is_rejected(self):
        # Guards the case where the hold fired so early that the migration
        # would be trivial and prove nothing.
        with self.assertRaises(AssertionError):
            assert_no_replay(self, clean_log(3))


class HoldCannotDecideTheOutcomeTests(unittest.TestCase):
    """The hold controls timing only."""

    def test_assertions_read_only_the_audit_record(self):
        # assert_no_replay takes the audit log and nothing else: no session,
        # no sync directory, no hold position. It cannot consult the hold.
        import inspect

        signature = inspect.signature(assert_no_replay)
        self.assertEqual(list(signature.parameters), ["case", "lines"])

    def test_a_replayed_log_fails_at_every_hold_position(self):
        # Whatever the hold chose, a replayed log is still rejected.
        for split in (5, 10, 15):
            with self.subTest(hold_after=split):
                lines = clean_log(split)
                lines.extend(clean_log(split))
                with self.assertRaises(AssertionError):
                    assert_no_replay(self, lines)

    def test_harness_does_not_emit_proof_markers(self):
        # The hold must not be able to write the record it is judged by.
        from continuum import _harness

        source = __import__("inspect").getsource(_harness)
        for marker in ("ACTION ", "IDENTITY", "FINAL ", "ITER_"):
            self.assertNotIn(
                marker,
                source,
                f"the hold primitive emits {marker!r}, which the proof reads",
            )

    def test_auditor_remains_an_external_process(self):
        harness = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "tests"
            / "test_adversarial_restart.py"
        ).read_text(encoding="utf-8")
        # The auditor is spawned as its own process and fsyncs each line, so
        # neither source nor target can rewrite the record after the fact.
        self.assertIn("subprocess.Popen(", harness)
        self.assertIn("AUDITOR", harness)
        self.assertIn("flush", harness)


if __name__ == "__main__":
    unittest.main()
