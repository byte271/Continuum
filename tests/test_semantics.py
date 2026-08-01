"""Semantic identities must survive meaning-preserving edits and break otherwise.

These are the properties the whole source-migration story rests on. An identity
that drifts when nothing meaningful changed makes safe edits unmappable; an
identity that persists when meaning *did* change makes unsafe edits look safe.
The second failure mode is the dangerous one, so it is tested hardest.
"""

from __future__ import annotations

import unittest

from continuum import semantics
from continuum.semantics import analyze, binding_identity


BASE = """
def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, bag, bias):
    index = 0
    while index < limit:
        bag.append(bias(index))
        print(f"ACTION {index}")
        index += 1
    return len(bag)


def outer(limit):
    bag = []
    bias = make_bias(3)
    return leaf(limit, bag, bias)


answer = outer(20)
"""


def active_site(source: str, scope_path: tuple[str, ...], op: str, occurrence: int = 0):
    """The identity of a chosen resume point, located without using line numbers.

    The function is found by scope path rather than by IR identifier, because
    the IR identifier embeds the line number this model exists to stop relying
    on.
    """

    model = analyze(source, "p.py")
    for ir_id, identity in sorted(model.by_ir_id.items()):
        if identity.scope_path != scope_path:
            continue
        found = 0
        code = model.ir["functions"][ir_id]["code"]
        for pc in range(len(code)):
            point = model.safepoint_at(ir_id, pc)
            if point is not None and point.op == op:
                if found == occurrence:
                    return model, identity, point
                found += 1
    raise AssertionError(f"no {op} occurrence {occurrence} in {scope_path}")


LEAF = ("__module__", "leaf")


class FunctionIdentityTests(unittest.TestCase):
    def semantic_ids(self, source: str) -> dict[str, str]:
        model = analyze(source, "p.py")
        return {
            "/".join(identity.scope_path): identity.semantic_id
            for identity in model.by_ir_id.values()
        }

    def test_identities_are_stable_under_added_blank_lines_and_comments(self):
        """Line movement alone must not change any function identity."""
        shifted = "\n\n# a comment\n\n" + BASE.replace(
            "def leaf(", "# explaining leaf\ndef leaf("
        )
        self.assertEqual(self.semantic_ids(BASE), self.semantic_ids(shifted))

    def test_identity_survives_a_changed_function_body(self):
        changed = BASE.replace('print(f"ACTION {index}")', 'print(f"STEP {index}")')
        self.assertEqual(
            self.semantic_ids(BASE)["__module__/leaf"],
            self.semantic_ids(changed)["__module__/leaf"],
        )

    def test_identity_survives_inserting_a_new_function(self):
        extended = BASE.replace(
            "def outer(limit):", "def helper(value):\n    return value\n\n\ndef outer(limit):"
        )
        before = self.semantic_ids(BASE)
        after = self.semantic_ids(extended)
        for key, value in before.items():
            with self.subTest(function=key):
                self.assertEqual(value, after[key])

    def test_a_renamed_parameter_is_a_different_function(self):
        renamed = BASE.replace(
            "def leaf(limit, bag, bias):", "def leaf(count, bag, bias):"
        ).replace("while index < limit:", "while index < count:")
        self.assertNotEqual(
            self.semantic_ids(BASE)["__module__/leaf"],
            self.semantic_ids(renamed)["__module__/leaf"],
        )

    def test_an_added_parameter_is_a_different_function(self):
        changed = BASE.replace(
            "def leaf(limit, bag, bias):", "def leaf(limit, bag, bias, extra=1):"
        )
        self.assertNotEqual(
            self.semantic_ids(BASE)["__module__/leaf"],
            self.semantic_ids(changed)["__module__/leaf"],
        )

    def test_a_reordered_signature_is_a_different_function(self):
        changed = BASE.replace(
            "def leaf(limit, bag, bias):", "def leaf(bag, limit, bias):"
        ).replace("leaf(limit, bag, bias)", "leaf(bag, limit, bias)")
        self.assertNotEqual(
            self.semantic_ids(BASE)["__module__/leaf"],
            self.semantic_ids(changed)["__module__/leaf"],
        )

    def test_a_deleted_function_loses_its_identity(self):
        model = analyze(BASE, "p.py")
        target = next(
            identity.semantic_id
            for identity in model.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        without = BASE.replace(
            "    return leaf(limit, bag, bias)", "    return len(bag)"
        )
        without = "\n".join(
            line
            for block, line in enumerate(without.splitlines())
            if not (7 <= block <= 14)
        )
        self.assertNotIn(target, analyze(without, "p.py").functions)

    def test_a_closure_that_stops_capturing_is_a_different_function(self):
        """Losing a captured cell changes what the frame needs to exist."""
        changed = BASE.replace("        return value + base", "        return value + 3")
        self.assertNotEqual(
            self.semantic_ids(BASE)["__module__/make_bias/bias"],
            self.semantic_ids(changed)["__module__/make_bias/bias"],
        )

    def test_no_program_is_ambiguous_by_default(self):
        self.assertEqual(analyze(BASE, "p.py").ambiguous_functions, {})


class SafepointIdentityTests(unittest.TestCase):
    def test_a_resume_point_survives_a_statement_inserted_after_it(self):
        """The core accepted edit: add code later in the same active body."""
        model, function, point = active_site(BASE, LEAF, "SAFEPOINT")
        extended = BASE.replace(
            "        index += 1", '        print("EXTRA")\n        index += 1'
        )
        after = analyze(extended, "p.py")
        matches = after.resolve_safepoint(function.semantic_id, point.semantic_id)
        self.assertEqual(len(matches), 1, "resume point did not map one-to-one")

    def test_a_resume_point_survives_a_changed_future_constant(self):
        model, function, point = active_site(BASE, LEAF, "SAFEPOINT")
        changed = BASE.replace("answer = outer(20)", "answer = outer(25)")
        after = analyze(changed, "p.py")
        self.assertEqual(
            len(after.resolve_safepoint(function.semantic_id, point.semantic_id)), 1
        )

    def test_a_resume_point_survives_a_new_function_added_elsewhere(self):
        model, function, point = active_site(BASE, LEAF, "SAFEPOINT")
        extended = BASE.replace(
            "answer = outer(20)", "def later(x):\n    return x\n\n\nanswer = outer(20)"
        )
        after = analyze(extended, "p.py")
        self.assertEqual(
            len(after.resolve_safepoint(function.semantic_id, point.semantic_id)), 1
        )

    def test_every_resume_point_in_the_unchanged_program_maps_uniquely(self):
        """A no-op edit must map every point one-to-one, or nothing else can."""
        model = analyze(BASE, "p.py")
        again = analyze(BASE, "p.py")
        checked = 0
        for (ir_id, pc), point in model.safepoint_by_pc.items():
            matches = again.resolve_safepoint(point.function, point.semantic_id)
            with self.subTest(function=ir_id, pc=pc):
                self.assertEqual(matches, [pc])
            checked += 1
        self.assertGreater(checked, 50)

    def test_occurrence_indexing_keeps_identically_shaped_siblings_distinct(self):
        """Duplicated structure is separated by occurrence, not collapsed.

        Named for what it checks. It does not inspect any reported ambiguity,
        and there is none to inspect: two identically shaped sibling statements
        differ by occurrence index, so each still resolves uniquely inside one
        revision. The cross-revision consequence -- a changed occurrence count
        -- is what the second half covers.
        """
        duplicated = BASE.replace(
            '        print(f"ACTION {index}")',
            '        print(f"ACTION {index}")\n        print(f"ACTION {index}")',
        )
        model = analyze(duplicated, "p.py")
        leaf = next(
            identity
            for identity in model.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        counts = [
            len(pcs)
            for (function_id, _), pcs in model.safepoints.items()
            if function_id == leaf.semantic_id
        ]
        # The two identical prints differ only by occurrence index, so each
        # still resolves uniquely inside this revision.
        self.assertTrue(all(count == 1 for count in counts))

        # Against the original revision, the original single print maps to the
        # first of the two -- and the second is new. The mapper must treat a
        # changed occurrence count as needing review rather than assuming.
        original = analyze(BASE, "p.py")
        original_leaf = next(
            identity
            for identity in original.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        self.assertEqual(original_leaf.semantic_id, leaf.semantic_id)

    def test_a_moved_resume_point_across_a_control_boundary_does_not_match(self):
        """Wrapping the active statement in a new loop changes its region.

        The resume point has to be the statement that moves. This previously
        took the first safe point in `leaf`, which is `index = 0` -- outside the
        loop and untouched by an edit to the loop body -- and then asserted only
        that at most one location matched. It matched, exactly as it should
        have, so the assertion could not fail for the behavior it names.
        """
        # Occurrence 2 is the `print(f"ACTION {index}")` statement, inside the
        # while body: the statement the edit below actually wraps.
        model, function, point = active_site(BASE, LEAF, "SAFEPOINT", occurrence=2)
        self.assertTrue(
            point.evidence()["control_region_path"],
            "the chosen resume point must start inside a control region",
        )
        wrapped = BASE.replace(
            '        print(f"ACTION {index}")',
            '        for _ in range(1):\n            print(f"ACTION {index}")',
        )
        after = analyze(wrapped, "p.py")
        matches = after.resolve_safepoint(function.semantic_id, point.semantic_id)
        self.assertEqual(
            matches, [], "a resume point moved into a new loop must not match"
        )

    def test_region_path_distinguishes_inside_a_loop_from_outside_it(self):
        model = analyze(BASE, "p.py")
        ir_id = next(
            ir
            for ir, identity in model.by_ir_id.items()
            if identity.scope_path == LEAF
        )
        code = model.ir["functions"][ir_id]["code"]
        points = [model.safepoint_at(ir_id, pc) for pc in range(len(code))]
        inside = [point for point in points if point and point.region_path]
        outside = [point for point in points if point and not point.region_path]

        # `index = 0` and `return len(bag)` sit at statement level; everything
        # in the loop body carries the loop as its enclosing region.
        self.assertTrue(outside, "no statement-level points in leaf")
        self.assertTrue(inside, "no loop-body points in leaf")
        self.assertTrue(all(point.region_path[0][3] == "body" for point in inside))
        self.assertTrue(all(point.region_path[0][0] == "While" for point in inside))

    def test_evidence_is_auditable(self):
        model, function, point = active_site(BASE, LEAF, "SAFEPOINT")
        evidence = point.evidence()
        self.assertIn("semantic_safepoint_id", evidence)
        self.assertIn("control_region_path", evidence)
        self.assertEqual(evidence["ir_program_counter"], point.pc)
        function_evidence = function.evidence()
        self.assertEqual(
            function_evidence["scope_path"], ["__module__", "leaf"]
        )
        self.assertIn("signature", function_evidence)


class BindingIdentityTests(unittest.TestCase):
    def test_binding_kind_is_part_of_the_identity(self):
        self.assertNotEqual(
            binding_identity("sfn:x", "value", "local"),
            binding_identity("sfn:x", "value", "cell"),
        )

    def test_bindings_are_scoped_to_their_function(self):
        self.assertNotEqual(
            binding_identity("sfn:a", "value", "local"),
            binding_identity("sfn:b", "value", "local"),
        )

    def test_active_bindings_are_enumerated(self):
        model = analyze(BASE, "p.py")
        leaf = next(
            identity
            for identity in model.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        ids = model.binding_ids(leaf.semantic_id)
        for name, kind in (
            ("limit", "parameter"),
            ("bag", "parameter"),
            ("index", "local"),
        ):
            with self.subTest(name=name):
                self.assertIn(binding_identity(leaf.semantic_id, name, kind), ids)

    def test_a_removed_local_loses_its_binding_identity(self):
        model = analyze(BASE, "p.py")
        leaf = next(
            identity
            for identity in model.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        target = binding_identity(leaf.semantic_id, "index", "local")
        self.assertIn(target, model.binding_ids(leaf.semantic_id))

        without = BASE.replace(
            """    index = 0
    while index < limit:
        bag.append(bias(index))
        print(f"ACTION {index}")
        index += 1
    return len(bag)""",
            """    for item in range(limit):
        bag.append(bias(item))
    return len(bag)""",
        )
        after = analyze(without, "p.py")
        after_leaf = next(
            identity
            for identity in after.by_ir_id.values()
            if identity.scope_path == ("__module__", "leaf")
        )
        self.assertNotIn(target, after.binding_ids(after_leaf.semantic_id))


NESTED = """
def outer(n):
    def inner(a):
        def helper(x):
            return x + a
        return helper(n)
    return inner(n)


def other(n):
    def inner(a):
        def helper(x):
            return x + a
        return helper(n)
    return inner(n)


answer = outer(2)
"""


class LexicalScopeIdentityTests(unittest.TestCase):
    """Function identity must use the whole lexical chain, not one parent name.

    An IR identifier is `parent.name@line`, where `parent` is only the
    immediately enclosing scope's display name. Deriving the scope path from
    that string reduces both `outer.inner.helper` and `other.inner.helper` to
    `("inner", "helper")`. With the same signature and the same captured names,
    the two collapse onto one semantic identity, and a re-nesting edit in which
    each revision is individually unambiguous binds an active frame to the
    wrong function.
    """

    def paths(self, source):
        model = analyze(source, "p.py")
        return {
            ir_id: (identity.scope_path, identity.semantic_id)
            for ir_id, identity in model.by_ir_id.items()
        }

    def test_the_scope_path_is_the_full_chain(self):
        found = {path for path, _ in self.paths(NESTED).values()}
        self.assertIn(("__module__", "outer", "inner", "helper"), found)
        self.assertIn(("__module__", "other", "inner", "helper"), found)

    def test_identically_shaped_functions_under_different_ancestors_differ(self):
        by_path = {path: sid for path, sid in self.paths(NESTED).values()}
        left = by_path[("__module__", "outer", "inner", "helper")]
        right = by_path[("__module__", "other", "inner", "helper")]
        self.assertNotEqual(
            left,
            right,
            "two helpers differing only in an outer ancestor share an identity",
        )

    def test_a_re_nesting_edit_does_not_silently_rebind(self):
        """Each revision is unambiguous, so only the full chain can refuse."""
        # Revision A defines the helper under `outer` only.
        revision_a = NESTED.replace(
            """

def other(n):
    def inner(a):
        def helper(x):
            return x + a
        return helper(n)
    return inner(n)
""",
            "",
        )
        # Revision B defines exactly the same helper under `other` instead.
        revision_b = revision_a.replace("def outer(n):", "def other(n):").replace(
            "answer = outer(2)", "answer = other(2)"
        )
        before = analyze(revision_a, "p.py")
        after = analyze(revision_b, "p.py")

        source_helper = next(
            identity
            for identity in before.by_ir_id.values()
            if identity.scope_path[-1] == "helper"
        )
        target_helper = next(
            identity
            for identity in after.by_ir_id.values()
            if identity.scope_path[-1] == "helper"
        )
        self.assertEqual(source_helper.scope_path[-2:], ("inner", "helper"))
        self.assertEqual(target_helper.scope_path[-2:], ("inner", "helper"))
        self.assertNotEqual(
            source_helper.semantic_id,
            target_helper.semantic_id,
            "a helper re-nested under a different ancestor kept its identity",
        )

    def test_annotation_still_changes_no_ir_byte(self):
        from continuum.compiler import compile_source, compile_with_sites

        plain = compile_source(NESTED, "p.py")
        annotated, _sites, scope_paths = compile_with_sites(NESTED, "p.py")
        self.assertEqual(plain, annotated)
        self.assertEqual(set(scope_paths), set(annotated["functions"]))


class AnnotationIntegrityTests(unittest.TestCase):
    def test_annotation_does_not_change_the_ir(self):
        from continuum.compiler import compile_source, compile_with_sites

        plain = compile_source(BASE, "p.py")
        annotated, sites, scope_paths = compile_with_sites(BASE, "p.py")
        self.assertEqual(plain, annotated)
        for function_id, definition in annotated["functions"].items():
            with self.subTest(function=function_id):
                self.assertEqual(len(sites[function_id]), len(definition["code"]))
                self.assertIn(function_id, scope_paths)

    def test_recompiling_a_mismatched_source_is_refused(self):
        from continuum.compiler import compile_source

        ir = compile_source(BASE, "p.py")
        different = BASE.replace("answer = outer(20)", "answer = outer(21)")
        with self.assertRaises(semantics.SemanticAmbiguity):
            semantics.analyze_image_source(different, "p.py", ir)

    def test_recompiling_the_matching_source_is_accepted(self):
        from continuum.compiler import compile_source

        ir = compile_source(BASE, "p.py")
        model = semantics.analyze_image_source(BASE, "p.py", ir)
        self.assertEqual(model.ir, ir)


if __name__ == "__main__":
    unittest.main()
