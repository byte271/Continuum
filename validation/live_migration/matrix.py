#!/usr/bin/env python3
"""Differential matrix for live source-code migration.

For every accepted revision pair this freezes revision A at *every* applicable
safe point, builds a migration plan, verifies it independently, applies it,
runs to completion under revision B, and judges the hybrid run against an
oracle stated separately from the migration implementation.

For refused, ambiguous, and tampered pairs it asserts a refusal happens at
every safe point, and records the reason code.

Testing one checkpoint proves almost nothing: a mapping can be correct at the
position it was developed against and wrong two instructions later. Sweeping
every safe point is what makes "accepted migrations are correct" a measurement
rather than an anecdote.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from continuum import migration  # noqa: E402
from continuum.compiler import compile_source  # noqa: E402
from continuum.errors import CompileError, ContinuumError  # noqa: E402
from continuum.image import load_image, save_image  # noqa: E402
from continuum.migration import MigrationRefused  # noqa: E402
from continuum.vm import VirtualMachine  # noqa: E402

PROGRAMS = Path(__file__).resolve().parent / "programs"

ACCEPTED_CORRECT = "accepted-and-correct"
CORRECTLY_REFUSED = "correctly-refused"
NOT_APPLICABLE = "no-live-frames-at-safe-point"
INFRASTRUCTURE = "infrastructure-failure"
SILENT_MISMATCH = "silent-incorrect-migration"
WRONGLY_ACCEPTED = "ambiguous-migration-accepted"
# Refused where acceptance was hoped for. A coverage limit, never corruption:
# the safe direction to be wrong in, and reported separately so it can never
# be mistaken for -- or hidden inside -- a correctness figure.
OVER_REFUSED = "explicitly-refused-narrower-than-hoped"

ACTION = re.compile(r"^ACTION (\d+) (\d+) (\d+)$")
NEWMARK = re.compile(r"^NEWMARK (\d+)$")
OLD_MARKER = re.compile(r"^FINAL \d+ \d+$")
NEW_MARKER = re.compile(r"^FINAL-V2 (\d+) (\d+)$")

# Oracle constants, derived by hand from revision_a.py.
LIMIT = 30
EXPECTED_TOTAL = 7 + sum(index + 3 for index in range(LIMIT))


def judge(prefix: str, suffix: str, expects_new_markers: bool) -> list[str]:
    """Every way this hybrid run differs from what the oracle requires.

    `expects_new_markers` says whether revision B actually changes future
    behavior. A no-op or future-only-function revision legitimately produces the
    revision-A markers, so demanding revision-B markers of it would be the
    harness asserting a falsehood rather than the migration being wrong.
    """

    failures: list[str] = []
    prefix_lines, suffix_lines = prefix.splitlines(), suffix.splitlines()
    combined = prefix_lines + suffix_lines

    prefix_actions = [line for line in prefix_lines if ACTION.match(line)]
    suffix_actions = [line for line in suffix_lines if ACTION.match(line)]
    actions = prefix_actions + suffix_actions

    indexes = sorted(int(ACTION.match(line).group(1)) for line in actions)
    if indexes != list(range(LIMIT)):
        failures.append(f"action indexes are not exactly 0..{LIMIT - 1}")
    if len(set(actions)) != len(actions):
        failures.append("an action nonce appeared more than once")
    repeated = set(prefix_actions) & set(suffix_actions)
    if repeated:
        failures.append(f"{len(repeated)} completed actions repeated")

    # If the marker statement already executed before the checkpoint, its edit
    # is in the past. The new marker cannot appear without replaying completed
    # work, which would be far worse than not observing the change. Continuum
    # accepts such an edit rather than refusing it -- see LIMITATIONS.md.
    already_emitted = any(OLD_MARKER.match(line) for line in prefix_lines)
    if already_emitted:
        expects_new_markers = False

    pattern = NEW_MARKER if expects_new_markers else OLD_MARKER
    forbidden = OLD_MARKER if expects_new_markers else NEW_MARKER
    if already_emitted:
        forbidden = NEW_MARKER
    if any(forbidden.match(line) for line in combined):
        failures.append("the wrong revision's future marker executed")
    markers = [line for line in combined if pattern.match(line)]
    if len(markers) != 1:
        failures.append(f"expected one final marker, saw {len(markers)}")
    else:
        total, count = markers[0].split()[1:3]
        if int(total) != EXPECTED_TOTAL:
            failures.append(f"final total {total} != oracle {EXPECTED_TOTAL}")
        if int(count) != LIMIT:
            failures.append("the shared alias or reference cycle did not survive")

    if any(NEWMARK.match(line) for line in prefix_lines):
        failures.append("new-revision behavior leaked into the prefix")
    marks = [line for line in suffix_lines if NEWMARK.match(line)]
    expected_marks = len(suffix_actions) if expects_new_markers else 0
    if already_emitted:
        expected_marks = 0
    if len(marks) != expected_marks:
        failures.append(
            f"NEWMARK count {len(marks)} != expected {expected_marks}"
        )
    return failures


def live_functions(source: str, safe_point: int) -> set[str]:
    """Names of the functions with a frame on the stack at this safe point."""

    vm = VirtualMachine(compile_source(source, "prog.py"), ["prog.py"], "prog.py")
    with contextlib.redirect_stdout(io.StringIO()):
        while vm.frames and not vm.completed and vm.safe_points_executed < safe_point:
            vm.step()
    return {vm.ir["functions"][frame.function_id]["name"] for frame in vm.frames}


def freeze_at(source: str, safe_point: int, image: Path) -> str | None:
    """Freeze at one safe point. Returns the prefix, or None if not applicable."""

    vm = VirtualMachine(compile_source(source, "prog.py"), ["prog.py"], "prog.py")
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        while vm.frames and not vm.completed and vm.safe_points_executed < safe_point:
            vm.step()
    if vm.completed or not vm.frames:
        return None
    save_image(image, vm, source)
    return stream.getvalue()


def total_safe_points(source: str) -> int:
    vm = VirtualMachine(compile_source(source, "prog.py"), ["prog.py"], "prog.py")
    with contextlib.redirect_stdout(io.StringIO()):
        vm.run()
    return vm.safe_points_executed


def migrate_and_run(image: Path, new_source: str, root: Path) -> str:
    """Plan, verify independently, apply, and finish. Returns the suffix."""

    candidate = root / "candidate.py"
    candidate.write_text(new_source, encoding="utf-8")
    plan = migration.plan_upgrade(image, candidate)
    plan_path = root / "plan.cup"
    migration.write_plan(
        plan_path, plan, new_source, compile_source(new_source, "prog.py")
    )
    migration.verify_plan(image, plan_path)
    stored, _source, new_ir = migration.read_plan(plan_path)
    vm = load_image(image).restore_vm()
    migration.apply_plan(vm, stored, new_ir)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        vm.run()
    return stream.getvalue()


NOT_YET_ACTIVE = "accepted-target-not-active-yet"


def sweep(
    revision_a: str,
    revision_b: str,
    pair: dict[str, Any],
    stride: int,
    root: Path,
) -> list[dict[str, Any]]:
    """Run one revision pair at every applicable safe point.

    A pair labelled "refuse" is only required to refuse where the element it
    damages is actually live. Deleting `middle` before `middle` has ever been
    called is an inactive-function edit, and accepting it is correct; treating
    that as a failure would be the harness misreading its own scenario.
    """

    expect = pair["expect"]
    requires_live = pair.get("requires_live")
    expects_new_markers = pair.get("expects_new_markers", True)
    cases: list[dict[str, Any]] = []
    total = total_safe_points(revision_a)
    points = range(1, total, stride) if stride > 1 else range(1, total)
    for safe_point in points:
        record: dict[str, Any] = {"safe_point": safe_point}
        image = root / f"image-{safe_point}.cont"
        try:
            prefix = freeze_at(revision_a, safe_point, image)
        except (CompileError, ContinuumError) as exc:
            record.update(
                classification=INFRASTRUCTURE, detail=f"freeze failed: {exc}"
            )
            cases.append(record)
            continue
        if prefix is None:
            record["classification"] = NOT_APPLICABLE
            cases.append(record)
            continue

        active = live_functions(revision_a, safe_point)
        effective = expect
        if expect == "refuse" and requires_live and requires_live not in active:
            effective = "accept-not-yet-active"
        record["live_frames"] = sorted(active)

        image_before = migration.sha256_file(image)
        try:
            suffix = migrate_and_run(image, revision_b, root)
        except MigrationRefused as exc:
            # Refusing is never silent corruption. Where a refusal was
            # required it is correct; where acceptance was hoped for it is a
            # narrower accepted-edit set than intended, which is a coverage
            # limit and is counted separately from correctness.
            required = effective in {"refuse", "accept-not-yet-active"}
            record.update(
                classification=CORRECTLY_REFUSED if required else OVER_REFUSED,
                reason=exc.reason,
                detail=exc.detail[:200],
            )
            cases.append(record)
            image.unlink(missing_ok=True)
            continue
        except Exception as exc:  # noqa: BLE001
            record.update(
                classification=INFRASTRUCTURE, detail=f"{type(exc).__name__}: {exc}"
            )
            cases.append(record)
            image.unlink(missing_ok=True)
            continue

        if effective == "refuse":
            record.update(
                classification=WRONGLY_ACCEPTED,
                detail="an edit that must be refused was accepted",
            )
            cases.append(record)
            image.unlink(missing_ok=True)
            continue
        if effective == "accept-not-yet-active":
            record["classification"] = NOT_YET_ACTIVE
            cases.append(record)
            image.unlink(missing_ok=True)
            continue

        failures = judge(prefix, suffix, expects_new_markers)
        if migration.sha256_file(image) != image_before:
            failures.append("the original image was modified")
        record["classification"] = ACCEPTED_CORRECT if not failures else SILENT_MISMATCH
        if failures:
            record["failures"] = failures
        cases.append(record)
        image.unlink(missing_ok=True)
    return cases


def build_pairs(revision_a: str, revision_b: str) -> list[dict[str, Any]]:
    """Accepted, refused, and maliciously ambiguous revision pairs."""

    return [
        {
            "name": "accepted: insert after resume point and change future marker",
            "source": revision_b,
            "expect": "accept",
        },
        {
            "name": "accepted: no-op revision",
            "source": revision_a,
            "expect": "accept",
            "expects_new_markers": False,
        },
        {
            "name": "accepted: add a future-only function",
            "source": revision_a.replace(
                "result = outer(30)",
                "def later(value):\n    return value\n\n\nresult = outer(30)",
            ),
            "expect": "accept",
            "expects_new_markers": False,
        },
        {
            "name": "refused: delete an active function",
            "requires_live": "middle",
            "source": revision_a.replace(
                "def middle(limit, tally, bias, graph):\n"
                "    return leaf(limit, tally, bias, graph)",
                "",
            ).replace(
                "answer = middle(limit, tally, bias, graph)",
                "answer = leaf(limit, tally, bias, graph)",
            ),
            "expect": "refuse",
        },
        {
            "name": "refused: change an active function signature",
            "requires_live": "leaf",
            "source": revision_a.replace(
                "def leaf(limit, tally, bias, graph):",
                "def leaf(limit, tally, bias, graph, extra=0):",
            ),
            "expect": "refuse",
        },
        {
            "name": "refused: rename a class member live instances use",
            "requires_live": "leaf",
            "source": revision_a.replace(
                "    def add(self, value):", "    def add2(self, value):"
            ).replace("tally.add(", "tally.add2("),
            "expect": "refuse",
        },
        {
            "name": "refused: ambiguous duplicated active function",
            "requires_live": "middle",
            "source": revision_a.replace(
                "def middle(limit, tally, bias, graph):",
                "def middle(limit, tally, bias, graph):\n"
                "    return leaf(limit, tally, bias, graph)\n\n\n"
                "def middle(limit, tally, bias, graph):",
            ),
            "expect": "refuse",
        },
        {
            "name": "refused: wrap the active loop in a conditional",
            "requires_live": "leaf",
            "source": revision_a.replace(
                "    index = 0\n    while index < limit:",
                "    index = 0\n    if limit > 0:\n        while index < limit:",
            ).replace(
                "        tally.add(bias(index))\n"
                "        graph[\"shared\"].append(index)\n"
                "        print(f\"ACTION {index} {tally.total} {random.randint(0, 999)}\")\n"
                "        index += 1\n"
                "    return tally.total",
                "            tally.add(bias(index))\n"
                "            graph[\"shared\"].append(index)\n"
                "            print(f\"ACTION {index} {tally.total} {random.randint(0, 999)}\")\n"
                "            index += 1\n"
                "    return tally.total",
            ),
            "expect": "refuse",
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision-a", default=str(PROGRAMS / "revision_a.py"))
    parser.add_argument("--revision-b", default=str(PROGRAMS / "revision_b.py"))
    parser.add_argument(
        "--stride",
        type=int,
        default=1,
        help="sweep every Nth safe point; 1 means every applicable one",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    revision_a = Path(args.revision_a).read_text(encoding="utf-8")
    revision_b = Path(args.revision_b).read_text(encoding="utf-8")
    pairs = build_pairs(revision_a, revision_b)

    started = time.monotonic()
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for pair in pairs:
            cases = sweep(revision_a, pair["source"], pair, args.stride, root)
            counts: dict[str, int] = {}
            for case in cases:
                counts[case["classification"]] = (
                    counts.get(case["classification"], 0) + 1
                )
            results.append(
                {
                    "pair": pair["name"],
                    "expect": pair["expect"],
                    "safe_points_swept": len(cases),
                    "counts": counts,
                    "reasons": sorted(
                        {case["reason"] for case in cases if "reason" in case}
                    ),
                    "failures": [
                        case for case in cases
                        if case["classification"]
                        in {SILENT_MISMATCH, WRONGLY_ACCEPTED, INFRASTRUCTURE}
                    ][:5],
                }
            )

    accepted = sum(
        entry["counts"].get(ACCEPTED_CORRECT, 0) for entry in results
    )
    refused = sum(entry["counts"].get(CORRECTLY_REFUSED, 0) for entry in results)
    mismatches = sum(entry["counts"].get(SILENT_MISMATCH, 0) for entry in results)
    wrongly = sum(entry["counts"].get(WRONGLY_ACCEPTED, 0) for entry in results)
    infrastructure = sum(entry["counts"].get(INFRASTRUCTURE, 0) for entry in results)
    over_refused = sum(entry["counts"].get(OVER_REFUSED, 0) for entry in results)

    report = {
        "revision_pairs": len(pairs),
        "stride": args.stride,
        "accepted_and_correct": accepted,
        "correctly_refused": refused,
        "explicitly_refused_narrower_than_hoped": over_refused,
        "silent_incorrect_migrations": mismatches,
        "ambiguous_migrations_accepted": wrongly,
        "infrastructure_failures": infrastructure,
        "accepted_migration_correctness": (
            1.0 if accepted + mismatches == 0 else accepted / (accepted + mismatches)
        ),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "pairs": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")

    summary = {key: value for key, value in report.items() if key != "pairs"}
    print(json.dumps(summary, indent=2, sort_keys=True))
    for entry in results:
        print(
            f"  {entry['expect']:7s} {entry['safe_points_swept']:4d} pts  "
            f"{entry['counts']}  {entry['pair']}"
        )
        for failure in entry["failures"]:
            print(f"      FAILURE {failure}", file=sys.stderr)
    if mismatches or wrongly or infrastructure:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
