# 0.5.0a1 — verified live source-code migration

Alpha. Builds on 0.4.0a1's cross-Python execution ABI. The image format is
unchanged at container format 0.2; this release adds a second artifact beside
it rather than altering the image.

## The headline

An image frozen from **revision A** can be resumed under **revision B** — a
modified version of the same program — with live execution state carried across
the edit. Verified simultaneously across operating system, architecture, Python
version, and source revision.

[Actions run 30682958879](https://github.com/byte271/Continuum/actions/runs/30682958879),
evaluated on GitHub's pull-request merge commit `d328397`, which is generated per run and is not reachable from any branch in this repository:

| Property | Result |
| --- | --- |
| Source | revision A, Linux x86_64, CPython 3.12.13 |
| Target | revision B, macOS arm64, CPython 3.13.14 (native) |
| Source exited and reaped before target read the image | yes |
| Image SHA-256 capture / arrival / after migration | `10597941…db4c`, identical |
| Active frames mapped | 4, total mapping |
| Active bindings mapped | 20 |
| Action nonces | 30, each exactly once |
| Completed actions repeated | 0 |
| Old revision's future behavior executed | no |
| New revision's future behavior executed | yes |
| Oracle failures | 0 |

## Semantic identities

The IR names a function `outer.bump@9` and a resume point by an integer index.
Across two revisions both are worthless: a blank line renames every function
below it. `continuum/semantics.py` adds `SemanticFunctionID`,
`SemanticBindingID`, `SemanticSafepointID`, and `SemanticControlRegionID`. None
is defined solely by a line number, AST child index, display name, integer
program counter, source hash, text similarity, or edit distance.

Instruction sites are recorded in a **side table, never in the IR**, so
annotation changes not one byte of a produced image.

## New commands

```
continuum plan-upgrade old.cont new_program.py -o migration.cup
continuum inspect-upgrade migration.cup
continuum verify-upgrade old.cont migration.cup
continuum resume old.cont --upgrade migration.cup
```

`plan-upgrade` produces a **total** mapping or refuses, naming the exact
unmappable element. Never partial. `verify-upgrade` does not trust the plan: it
independently re-derives the whole mapping from the image and the plan's own new
source and refuses on any difference. Neither executes the program. The original
`.cont` is never written to.

## Accepted edit classes

1. changed future constants or expressions
2. statements inserted strictly after the active resume point
3. changed functions that are not active
4. added future-only functions
5. changed future sections of an active function, when every active binding,
   stack value, control region, and continuation edge maps unambiguously

Everything else is refused.

## Evidence

Differential matrix, every applicable safe point, 8 revision pairs, 1,576 cases
(`compatibility/results/migration-matrix-linux-x86_64-2026-07-31.json`):

| Outcome | Cases |
| --- | ---: |
| Accepted and correct | 590 |
| Accepted; the damaged element was not yet live | 26 |
| Correctly refused | 959 |
| Refused, narrower than hoped | 1 |
| **Silent incorrect migrations** | **0** |
| **Ambiguous migrations accepted** | **0** |
| Infrastructure failures | 0 |
| **Total** | **1,576** |

The 26 accepted cases in the second row come from pairs labelled "refuse".
Such a pair is only required to refuse where the element it damages is
actually live: deleting `middle` before `middle` has ever been called is an
inactive-function edit, and accepting it is correct. They are listed
separately rather than folded into the first row because the oracle judges
them by a different rule.

**Accepted-migration correctness: 100%.**

535 tests green on CPython 3.12.13 and 3.13.14.

## A bug the sweep found

Sweeping every safe point, rather than the one checkpoint the code was written
against, found a real defect: an IR function identifier embeds a line number, so
inserting a line renames `middle@28` to `middle@29`. Frame positions were
remapped, but a `FunctionValue` held in a global or a closure carried its own
identifier and still named the old revision. Calling a not-yet-called function
after migrating raised `NameError`; had another function inherited that
identifier, it would have silently bound to the wrong code.

The earlier cross-platform proof passed only because its workload never called
through a renamed value after resuming. `apply_plan` now rewrites every
reachable identifier, and three regression tests reproduce the exact failure
when the fix is removed.

## Performance

Linux x86_64, 20,000 iterations, medians. See PERFORMANCE.md.

| Figure | 3.12.13 | 3.13.14 |
| --- | ---: | ---: |
| Slowdown vs. CPython | 144x | 206x |
| Safe-point overhead | +0.2% | within noise |
| Freeze | 7.0 ms | 6.7 ms |
| Plan generation | 9.7 ms | 13.8 ms |
| Plan verification | 11.2 ms | 16.7 ms |
| Resume (+ migration) | 2.0 ms | 2.2 ms |

Verification costs more than generation on purpose: it re-derives the mapping
rather than trusting the plan.

## Known gap

**An edit to a statement that already executed is accepted, not refused.** The
migration stays sound — nothing is replayed, nothing is corrupted — but the
author sees the old behavior with no diagnostic explaining why. Detecting this
in general needs to know which statements have run, which a resume position does
not tell you for code inside a loop. This is a mandatory refusal case that is
not implemented.

## Not claimed

Arbitrary source changes, arbitrary hot reload, arbitrary Python versions,
arbitrary process migration, native CPython frame migration, thread, socket,
subprocess, or native-extension-state migration, any verified Windows
cross-platform path, or migration on any platform pair other than
Linux x86_64 → macOS arm64.
