"""Accepted migrations must be correct; everything unprovable must be refused.

The accepted cases here check that live state really moves onto the new
revision. The refusal cases matter more: each one is an edit that a naive
line-based or text-similarity mapper would happily accept and silently corrupt.
Every refusal must name the exact element that could not be mapped.

Nothing in this file executes a user program during planning or verification.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from continuum import migration
from continuum.compiler import compile_source
from continuum.image import load_image, save_image
from continuum.migration import MigrationRefused
from continuum.vm import VirtualMachine

REVISION_A = '''
class Tally:
    def __init__(self, seed):
        self.total = seed

    def add(self, value):
        self.total = self.total + value


def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, tally, bias, graph):
    index = 0
    while index < limit:
        tally.add(bias(index))
        graph["shared"].append(index)
        print(f"ACTION {index} {tally.total}")
        index += 1
    return tally.total


def middle(limit, tally, bias, graph):
    return leaf(limit, tally, bias, graph)


def outer(limit):
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    tally = Tally(7)
    bias = make_bias(3)
    answer = middle(limit, tally, bias, graph)
    print(f"FINAL {answer} {len(shared)}")
    return answer


result = outer(30)
'''

# Accepted: a statement inserted strictly after the active resume point, and a
# changed future expression. Both visibly change future behavior.
REVISION_B = REVISION_A.replace(
    '        print(f"ACTION {index} {tally.total}")',
    '        print(f"ACTION {index} {tally.total}")\n        print(f"NEWMARK {index}")',
).replace('print(f"FINAL {answer}', 'print(f"FINAL-V2 {answer}')

CHECKPOINT_SAFE_POINTS = 60


class MigrationCase(unittest.TestCase):
    """One real frozen image with four active frames, reused by every case."""

    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.image = cls.root / "a.cont"
        vm = VirtualMachine(
            compile_source(REVISION_A, "prog.py"), ["prog.py"], "prog.py"
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            while vm.frames and vm.safe_points_executed < CHECKPOINT_SAFE_POINTS:
                vm.step()
        assert len(vm.frames) == 4, len(vm.frames)
        save_image(cls.image, vm, REVISION_A)
        cls.prefix = stream.getvalue()
        cls.image_sha = migration.sha256_file(cls.image)

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def write_revision(self, source: str, name: str) -> Path:
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def plan_for(self, source: str, name: str = "candidate.py"):
        return migration.plan_upgrade(self.image, self.write_revision(source, name))

    def assertRefused(self, source: str, reason: str, name: str = "bad.py"):
        with self.assertRaises(MigrationRefused) as caught:
            self.plan_for(source, name)
        self.assertEqual(caught.exception.reason, reason, caught.exception.detail)
        # A refusal is only useful if it names the blocking element.
        self.assertTrue(caught.exception.element, "refusal named no element")
        return caught.exception


class AcceptedMigrationTests(MigrationCase):
    def test_the_image_is_never_modified_by_planning(self):
        self.plan_for(REVISION_B)
        self.assertEqual(migration.sha256_file(self.image), self.image_sha)

    def test_every_active_frame_is_mapped(self):
        plan = self.plan_for(REVISION_B)
        self.assertEqual(plan["active_frames"], 4)
        self.assertTrue(plan["mapping_is_total"])
        depths = [m["frame_depth"] for m in plan["frame_mappings"]]
        self.assertEqual(depths, [0, 1, 2, 3])

    def test_the_active_resume_point_moves_and_the_others_do_not(self):
        """Only the frame whose body gained a statement should shift."""
        plan = self.plan_for(REVISION_B)
        moved = [m for m in plan["frame_mappings"] if m["old_pc"] != m["new_pc"]]
        self.assertEqual(len(moved), 1)
        self.assertEqual(
            moved[0]["evidence"]["old_function"]["scope_path"],
            ["__module__", "leaf"],
        )

    def test_bindings_classes_and_edits_are_recorded(self):
        plan = self.plan_for(REVISION_B)
        self.assertGreater(len(plan["binding_mappings"]), 0)
        self.assertEqual(len(plan["class_mappings"]), 1)
        self.assertEqual(plan["class_mappings"][0]["members"], ["__init__", "add"])
        self.assertIn(
            "inserted-statements-after-the-active-resume-point",
            plan["accepted_edit_classes"],
        )

    def test_every_mapping_carries_auditable_evidence(self):
        plan = self.plan_for(REVISION_B)
        for mapping in plan["frame_mappings"]:
            with self.subTest(depth=mapping["frame_depth"]):
                evidence = mapping["evidence"]
                self.assertIn("semantic_function_id", evidence["old_function"])
                self.assertIn("signature", evidence["old_function"])
                self.assertIn("semantic_safepoint_id", evidence["resume_point"])
                self.assertIn("control_region_path", evidence["resume_point"])

    def test_a_no_op_revision_maps_every_frame_in_place(self):
        plan = self.plan_for(REVISION_A, "same.py")
        for mapping in plan["frame_mappings"]:
            with self.subTest(depth=mapping["frame_depth"]):
                self.assertEqual(mapping["old_pc"], mapping["new_pc"])

    def test_adding_a_future_only_function_is_accepted(self):
        source = REVISION_A.replace(
            "result = outer(30)", "def later(x):\n    return x * 2\n\n\nresult = outer(30)"
        )
        plan = self.plan_for(source, "added.py")
        self.assertIn("added-future-only-functions", plan["accepted_edit_classes"])

    def test_editing_a_loop_body_statement_is_an_accepted_future_edit(self):
        """The frame is paused on the loop back-edge, so the body is future code.

        Execution resumes by jumping to the loop test and re-entering the body,
        which means a changed body statement runs in its new form on the next
        iteration. This is accepted deliberately, and is exactly why the resume
        point's identity must not depend on the body's contents.
        """
        source = REVISION_A.replace(
            '        graph["shared"].append(index)',
            '        graph["shared"].append(index)\n        pass',
        )
        plan = self.plan_for(source, "bodyedit.py")
        self.assertTrue(plan["mapping_is_total"])

    def test_changing_an_inactive_function_is_accepted(self):
        """`make_bias` has already returned, so its body may change freely."""
        source = REVISION_A.replace(
            "        return value + base", "        return value + base + 0"
        )
        plan = self.plan_for(source, "inactive.py")
        self.assertTrue(plan["mapping_is_total"])


class ExecutedMigrationTests(MigrationCase):
    """The mapping must actually produce the right hybrid execution."""

    def migrate_and_run(self, source: str, name: str):
        plan = self.plan_for(source, name)
        plan_path = self.root / f"{name}.cup"
        migration.write_plan(
            plan_path, plan, source, compile_source(source, "prog.py")
        )
        migration.verify_plan(self.image, plan_path)
        stored, _new_source, new_ir = migration.read_plan(plan_path)
        vm = load_image(self.image).restore_vm()
        migration.apply_plan(vm, stored, new_ir)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            vm.run()
        return self.prefix, stream.getvalue()

    def test_the_hybrid_run_matches_an_independently_specified_oracle(self):
        prefix, suffix = self.migrate_and_run(REVISION_B, "exec_b.py")
        combined = prefix + suffix
        lines = combined.splitlines()

        # Oracle, stated here rather than derived from the implementation.
        actions = [line for line in lines if line.startswith("ACTION")]
        marks = [line for line in lines if line.startswith("NEWMARK")]
        prefix_actions = [
            line for line in prefix.splitlines() if line.startswith("ACTION")
        ]

        self.assertEqual(len(actions), 30, "every iteration must run exactly once")
        self.assertEqual(len(set(actions)), 30, "an action nonce repeated")
        self.assertEqual(
            len(set(prefix_actions) & set(suffix.splitlines())),
            0,
            "completed work was replayed",
        )
        # New behavior runs only from the resume point onward.
        self.assertEqual(len(marks), 30 - len(prefix_actions))
        self.assertNotIn(
            "NEWMARK", prefix, "new behavior leaked into the pre-migration prefix"
        )
        # Old future behavior must not run; new future behavior must.
        self.assertFalse(any(line.startswith("FINAL ") for line in lines))
        self.assertTrue(any(line.startswith("FINAL-V2") for line in lines))
        # 7 + sum(i + 3 for i in range(30)) = 7 + 435 + 90 = 532
        self.assertEqual(lines[-1], "FINAL-V2 532 30")

    def test_the_image_is_unchanged_after_a_migrated_resume(self):
        self.migrate_and_run(REVISION_B, "exec_unchanged.py")
        self.assertEqual(migration.sha256_file(self.image), self.image_sha)

    def test_shared_reference_and_cycle_survive_the_migration(self):
        prefix, suffix = self.migrate_and_run(REVISION_B, "exec_graph.py")
        # `graph["shared"]` is the same list as `shared`; the final line reports
        # its length, which is only 30 if the alias survived the crossing.
        self.assertTrue((prefix + suffix).strip().endswith(" 30"))


class RefusedMigrationTests(MigrationCase):
    def test_deleting_an_active_function_is_refused(self):
        source = REVISION_A.replace(
            "def middle(limit, tally, bias, graph):\n"
            "    return leaf(limit, tally, bias, graph)",
            "",
        ).replace(
            "answer = middle(limit, tally, bias, graph)",
            "answer = leaf(limit, tally, bias, graph)",
        )
        self.assertRefused(source, migration.REFUSE_ACTIVE_FUNCTION_MISSING)

    def test_changing_an_active_function_signature_is_refused(self):
        source = REVISION_A.replace(
            "def leaf(limit, tally, bias, graph):",
            "def leaf(limit, tally, bias, graph, extra=0):",
        )
        self.assertRefused(source, migration.REFUSE_ACTIVE_FUNCTION_MISSING)

    def test_renaming_an_active_parameter_is_refused(self):
        source = REVISION_A.replace(
            "def leaf(limit, tally, bias, graph):",
            "def leaf(count, tally, bias, graph):",
        ).replace("while index < limit:", "while index < count:")
        self.assertRefused(source, migration.REFUSE_ACTIVE_FUNCTION_MISSING)

    def test_removing_an_active_local_binding_is_refused(self):
        source = REVISION_A.replace(
            """    index = 0
    while index < limit:
        tally.add(bias(index))
        graph["shared"].append(index)
        print(f"ACTION {index} {tally.total}")
        index += 1
    return tally.total""",
            """    while True:
        break
    return tally.total""",
        )
        self.assertRefused(source, migration.REFUSE_SAFEPOINT_UNMAPPABLE)

    def test_moving_the_active_location_across_a_control_boundary_is_refused(self):
        """Wrapping the active loop in a conditional changes its region path."""
        source = REVISION_A.replace(
            """    index = 0
    while index < limit:
        tally.add(bias(index))
        graph["shared"].append(index)
        print(f"ACTION {index} {tally.total}")
        index += 1
    return tally.total""",
            """    index = 0
    if limit > 0:
        while index < limit:
            tally.add(bias(index))
            graph["shared"].append(index)
            print(f"ACTION {index} {tally.total}")
            index += 1
    return tally.total""",
        )
        self.assertRefused(source, migration.REFUSE_SAFEPOINT_UNMAPPABLE)

    def test_changing_the_active_loop_header_is_refused(self):
        source = REVISION_A.replace(
            "    while index < limit:", "    while index < limit + 0:"
        )
        self.assertRefused(source, migration.REFUSE_SAFEPOINT_UNMAPPABLE)

    def test_an_incompatible_class_layout_is_refused(self):
        source = REVISION_A.replace(
            "    def add(self, value):\n        self.total = self.total + value",
            "    def accumulate(self, value):\n        self.total = self.total + value",
        ).replace("tally.add(", "tally.accumulate(")
        self.assertRefused(source, migration.REFUSE_CLASS_LAYOUT_CHANGED)

    def test_removing_a_class_member_is_refused(self):
        source = REVISION_A.replace(
            "    def add(self, value):\n        self.total = self.total + value\n", ""
        ).replace("        tally.add(bias(index))", "        pass")
        self.assertRefused(source, migration.REFUSE_CLASS_LAYOUT_CHANGED)

    def test_changing_an_active_closure_cell_is_refused(self):
        """`bias` closes over `base`; dropping the capture changes the closure."""
        source = REVISION_A.replace(
            "        return value + base", "        return value + 3"
        )
        with self.assertRaises(MigrationRefused) as caught:
            self.plan_for(source, "closure.py")
        self.assertIn(
            caught.exception.reason,
            {
                migration.REFUSE_ACTIVE_FUNCTION_MISSING,
                migration.REFUSE_CELL_MISSING,
                migration.REFUSE_LIVE_FUNCTION_MISSING,
                migration.REFUSE_SAFEPOINT_UNMAPPABLE,
            },
        )

    def test_a_syntactically_invalid_revision_is_refused(self):
        from continuum.errors import CompileError

        with self.assertRaises(CompileError):
            self.plan_for("def broken(:\n    pass\n", "broken.py")

    def test_a_duplicated_active_function_is_refused_as_ambiguous(self):
        """Two definitions with one identity give no single place to resume."""
        source = REVISION_A.replace(
            "def middle(limit, tally, bias, graph):",
            "def middle(limit, tally, bias, graph):\n"
            "    return leaf(limit, tally, bias, graph)\n\n\n"
            "def middle(limit, tally, bias, graph):",
        )
        self.assertRefused(source, migration.REFUSE_ACTIVE_FUNCTION_AMBIGUOUS, "ambig.py")

    def test_a_renamed_class_method_is_refused_as_a_layout_change(self):
        source = REVISION_A.replace(
            "    def add(self, value):", "    def add2(self, value):"
        ).replace("tally.add(", "tally.add2(")
        self.assertRefused(source, migration.REFUSE_CLASS_LAYOUT_CHANGED, "meth.py")


class PlanIntegrityTests(MigrationCase):
    """A plan is untrusted content. Tampering must be refused, not applied."""

    def make_plan(self, name="integrity.cup"):
        plan = self.plan_for(REVISION_B, "integrity.py")
        path = self.root / name
        migration.write_plan(
            path, plan, REVISION_B, compile_source(REVISION_B, "prog.py")
        )
        return path

    def rewrite(self, path: Path, mutate, name: str) -> Path:
        with zipfile.ZipFile(path, "r") as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        mutate(entries)
        # Recompute every checksum, so the artifact is a well-formed lie.
        entries["checksums.json"] = json.dumps(
            {
                "algorithm": "sha256",
                "entries": {
                    n: migration._sha256(c)
                    for n, c in sorted(entries.items())
                    if n != "checksums.json"
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target = self.root / name
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry, content in sorted(entries.items()):
                archive.writestr(entry, content)
        return target

    def patch_plan(self, entries, mutate):
        document = json.loads(entries["plan.json"])
        mutate(document)
        entries["plan.json"] = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def test_a_valid_plan_verifies(self):
        report = migration.verify_plan(self.image, self.make_plan())
        self.assertEqual(report["integrity"], "verified")
        self.assertTrue(report["independently_rederived"])
        self.assertEqual(report["execution"], "not started")

    def test_a_broken_checksum_is_refused(self):
        path = self.make_plan("broken.cup")
        with zipfile.ZipFile(path, "r") as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        entries["new_source.py"] = b"print('substituted')\n"
        target = self.root / "broken-out.cup"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry, content in sorted(entries.items()):
                archive.writestr(entry, content)
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_a_remapped_frame_with_recomputed_checksums_is_refused(self):
        """The core tampering case: move a resume point and fix the hashes."""

        def mutate(entries):
            self.patch_plan(
                entries,
                lambda plan: plan["frame_mappings"][3].__setitem__("new_pc", 0),
            )

        target = self.rewrite(self.make_plan("remap.cup"), mutate, "remap-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.verify_plan(self.image, target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_a_tampered_binding_mapping_is_refused(self):
        def mutate(entries):
            self.patch_plan(
                entries,
                lambda plan: plan["binding_mappings"].append(
                    {
                        "frame_depth": 3,
                        "name": "injected",
                        "kind": "local",
                        "semantic_binding_id": "sbd:0",
                    }
                ),
            )

        target = self.rewrite(self.make_plan("bind.cup"), mutate, "bind-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.verify_plan(self.image, target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_a_tampered_class_mapping_is_refused(self):
        def mutate(entries):
            self.patch_plan(
                entries,
                lambda plan: plan["class_mappings"][0].__setitem__(
                    "members", ["__init__"]
                ),
            )

        target = self.rewrite(self.make_plan("cls.cup"), mutate, "cls-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.verify_plan(self.image, target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_an_unknown_plan_version_is_refused(self):
        def mutate(entries):
            self.patch_plan(
                entries, lambda plan: plan.update(plan_format_version="99.0")
            )

        target = self.rewrite(self.make_plan("ver.cup"), mutate, "ver-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(target)
        self.assertEqual(
            caught.exception.reason, migration.REFUSE_UNKNOWN_PLAN_VERSION
        )

    def test_an_unknown_execution_abi_is_refused(self):
        def mutate(entries):
            self.patch_plan(
                entries, lambda plan: plan.update(execution_abi_version="99.0")
            )

        target = self.rewrite(self.make_plan("abi.cup"), mutate, "abi-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(target)
        self.assertEqual(
            caught.exception.reason, migration.REFUSE_UNKNOWN_EXECUTION_ABI
        )

    def test_a_substituted_new_source_is_refused(self):
        """Swapping the source and its declared hash is caught by re-derivation."""

        def mutate(entries):
            substituted = REVISION_A.replace(
                'print(f"FINAL {answer}', 'print(f"HIJACKED {answer}'
            )
            entries["new_source.py"] = substituted.encode("utf-8")
            entries["new_ir.json"] = json.dumps(
                compile_source(substituted, "prog.py"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.patch_plan(
                entries,
                lambda plan: plan.update(
                    new_source_sha256=migration._sha256(entries["new_source.py"]),
                    new_ir_sha256=migration._sha256(entries["new_ir.json"]),
                ),
            )

        target = self.rewrite(self.make_plan("sub.cup"), mutate, "sub-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.verify_plan(self.image, target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_a_plan_for_a_different_image_is_refused(self):
        path = self.make_plan("other.cup")
        other = self.root / "other.cont"
        vm = VirtualMachine(
            compile_source(REVISION_A, "prog.py"), ["prog.py"], "prog.py"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            while vm.frames and vm.safe_points_executed < 30:
                vm.step()
        save_image(other, vm, REVISION_A)
        with self.assertRaises(MigrationRefused) as caught:
            migration.verify_plan(other, path)
        self.assertEqual(
            caught.exception.reason, migration.REFUSE_IMAGE_HASH_MISMATCH
        )

    def test_a_duplicate_archive_entry_is_refused(self):
        path = self.make_plan("dup.cup")
        with zipfile.ZipFile(path, "r") as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        target = self.root / "dup-out.cup"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry, content in sorted(entries.items()):
                archive.writestr(entry, content)
            archive.writestr("plan.json", entries["plan.json"])
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_PLAN_TAMPERED)

    def test_an_unexpected_extra_entry_is_refused(self):
        def mutate(entries):
            entries["payload.bin"] = b"unexpected"

        target = self.rewrite(self.make_plan("extra.cup"), mutate, "extra-out.cup")
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(target)
        self.assertEqual(caught.exception.reason, migration.REFUSE_MALFORMED_PLAN)

    def test_applying_a_plan_to_the_wrong_position_is_refused(self):
        stored, _source, new_ir = migration.read_plan(self.make_plan("pos.cup"))
        vm = load_image(self.image).restore_vm()
        vm.frames[3].pc += 1
        with self.assertRaises(MigrationRefused) as caught:
            migration.apply_plan(vm, stored, new_ir)
        self.assertEqual(caught.exception.reason, migration.REFUSE_MALFORMED_PLAN)

    def test_not_a_zip_is_refused(self):
        path = self.root / "garbage.cup"
        path.write_bytes(b"not an archive at all")
        with self.assertRaises(MigrationRefused) as caught:
            migration.read_plan(path)
        self.assertEqual(caught.exception.reason, migration.REFUSE_MALFORMED_PLAN)


class PlanContentTests(MigrationCase):
    def test_the_plan_records_every_required_field(self):
        plan = self.plan_for(REVISION_B, "fields.py")
        for field in (
            "plan_format_version",
            "semantic_model_version",
            "execution_abi_version",
            "original_image_sha256",
            "old_source_sha256",
            "old_ir_sha256",
            "new_source_sha256",
            "new_ir_sha256",
            "frame_mappings",
            "binding_mappings",
            "control_region_mappings",
            "class_mappings",
            "accepted_edit_classes",
            "assumptions",
            "mapping_is_total",
        ):
            with self.subTest(field=field):
                self.assertIn(field, plan)

    def test_the_plan_hashes_identify_the_real_inputs(self):
        plan = self.plan_for(REVISION_B, "hashes.py")
        self.assertEqual(plan["original_image_sha256"], self.image_sha)
        self.assertEqual(
            plan["old_source_sha256"], migration._sha256(REVISION_A.encode("utf-8"))
        )
        self.assertEqual(
            plan["new_source_sha256"], migration._sha256(REVISION_B.encode("utf-8"))
        )

    def test_assumptions_are_stated_explicitly(self):
        plan = self.plan_for(REVISION_B, "assume.py")
        self.assertTrue(plan["assumptions"])
        self.assertTrue(
            any("Completed effects" in item for item in plan["assumptions"])
        )


if __name__ == "__main__":
    unittest.main()


class LiveIdentifierRewriteTests(MigrationCase):
    """Values reachable after a migration must name the new revision's code.

    An IR function identifier embeds a line number, so inserting one line above
    a function renames it. A `FunctionValue` in a global or a closure carries
    that identifier itself, and a frame remap does not touch it. Before this was
    fixed, migrating and then calling a not-yet-called function raised NameError
    for an identifier that no longer existed -- found by sweeping every safe
    point rather than by testing a single checkpoint.
    """

    def test_the_plan_maps_every_renamed_function_identifier(self):
        plan = self.plan_for(REVISION_B, "ids.py")
        mappings = plan["function_id_mappings"]
        renamed = {
            old: new for old, new in mappings.items() if old != new
        }
        self.assertTrue(
            renamed, "the fixture no longer renames any function identifier"
        )
        for old, new in mappings.items():
            with self.subTest(function=old):
                self.assertIn(new, compile_source(REVISION_B, "prog.py")["functions"])

    def test_a_migrated_vm_holds_no_stale_identifier(self):
        from continuum.values import BoundMethodValue, ClassValue, FunctionValue

        plan = self.plan_for(REVISION_B, "stale.py")
        plan_path = self.root / "stale.cup"
        migration.write_plan(
            plan_path, plan, REVISION_B, compile_source(REVISION_B, "prog.py")
        )
        stored, _source, new_ir = migration.read_plan(plan_path)
        vm = load_image(self.image).restore_vm()
        migration.apply_plan(vm, stored, new_ir)

        seen: set[int] = set()
        checked = {"functions": 0}

        def walk(value):
            if id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, FunctionValue):
                checked["functions"] += 1
                self.assertIn(
                    value.function_id,
                    new_ir["functions"],
                    f"stale function identifier {value.function_id}",
                )
                for item in (*value.closure, *value.defaults, *value.kw_defaults):
                    walk(item)
            elif isinstance(value, BoundMethodValue):
                walk(value.function)
                walk(value.instance)
            elif isinstance(value, ClassValue):
                for member in value.members.values():
                    walk(member)
            elif isinstance(value, dict):
                for key, item in value.items():
                    walk(key)
                    walk(item)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    walk(item)
            elif hasattr(value, "__dict__"):
                for item in vars(value).values():
                    walk(item)

        walk(vm.globals)
        for frame in vm.frames:
            walk(frame.locals)
            walk(frame.stack)
            walk(frame.cells)
        self.assertGreater(checked["functions"], 0, "no function values reached")

    def test_a_function_first_called_after_migration_resolves(self):
        """The regression itself: freeze before the call, migrate, then call."""
        early = self.root / "early.cont"
        vm = VirtualMachine(
            compile_source(REVISION_A, "prog.py"), ["prog.py"], "prog.py"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            # Safe point 8: the module frame has built the function values but
            # has not yet called through them. This is the exact position where
            # the full sweep raised NameError for a renamed identifier.
            while vm.frames and vm.safe_points_executed < 8:
                vm.step()
        save_image(early, vm, REVISION_A)

        candidate = self.root / "early_b.py"
        candidate.write_text(REVISION_B, encoding="utf-8")
        plan = migration.plan_upgrade(early, candidate)
        plan_path = self.root / "early.cup"
        migration.write_plan(
            plan_path, plan, REVISION_B, compile_source(REVISION_B, "prog.py")
        )
        stored, _source, new_ir = migration.read_plan(plan_path)
        restored = load_image(early).restore_vm()
        migration.apply_plan(restored, stored, new_ir)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            restored.run()
        output = stream.getvalue()
        self.assertIn("FINAL-V2", output)
        self.assertEqual(
            len([l for l in output.splitlines() if l.startswith("ACTION")]), 30
        )
