# Compatibility

## The execution compatibility contract (container format 0.2)

Container format 0.2 images carry an `execution_contract` block that separates
the axes the 0.1 container collapsed into two fields. Each is versioned
independently:

| Axis | Meaning | Gates a restore? |
| --- | --- | --- |
| `container_format_version` | archive layout | yes, exact |
| `graph_codec_version` | object-graph encoding | yes, exact |
| `ir_version` | instruction set the frames index | yes, exact |
| `execution_abi_version` | meaning of frame, binding, stack, and control state | yes, exact |
| `creator.continuum_version` | which Continuum wrote the image | no — provenance |
| `creator.python_version` | which interpreter wrote the image | no — provenance |
| `target.runtime_implementations` | which runtimes may restore it | yes |
| `target.python_versions` | interpreters the creator accepts | yes |
| `target.required_capabilities` | named features the target must implement | yes |

Creator identity is recorded but does not gate the restore. What gates it is
the execution ABI, the capability set, and the interpreter allowlist.

The interpreter decision has **two independent gates**, and both must pass:

1. the running interpreter appears in the image's `target.python_versions`;
2. the running interpreter appears in this runtime's
   `abi.VERIFIED_PYTHON_VERSIONS`.

The second gate means an image cannot widen what this runtime accepts by
asserting a version nobody verified. Membership in `VERIFIED_PYTHON_VERSIONS`
requires a green native cross-Python proof run, so it is a record of evidence
rather than an intention.

The allowlist is **exact and never a range**. `3.13.0` and `3.12.14` are
refused exactly as firmly as `3.9`, even though both sit inside the interval the
verified versions span. Packaging metadata (`requires-python`) is necessarily
coarser than an exact allowlist; it is an install-time filter only, and the
runtime allowlist is the authority. Both halves of that split are tested.

Every refusal carries a stable machine-readable reason code, so compatibility
policy is asserted on directly rather than by matching prose.

### Format 0.1 images

Format 0.1 images carry no contract, so nothing in them would justify a
capability-based decision. They keep their original rule — exact creator Python
*and* exact creator Continuum version — and their refusal messages name the
format version and state that re-freezing under 0.2 is what provides
cross-Python restore. An image cannot obtain the 0.2 policy by declaring 0.1
while carrying contract fields, nor the reverse.

## Cross-Python differential corpus

The corpus below measures *unchanged-source* behavior on one interpreter. A
separate paired suite measures whether live execution state survives a change
of interpreter: `validation/cross_python/differential.py` freezes each program
at safe points spread across its execution, deeply verifies the image without
executing it, restores under the other interpreter, and compares against an
independently run uninterrupted control.

The comparison covers the logical frame chain, resume positions and opcodes,
locals, lexical cells, operand stacks, control blocks, pending finally state,
module globals, module RNG state, `random.Random` state, file offsets, and
instruction and safe-point counters. Object identity is compared structurally:
each object is labelled on first visit and revisits emit a back-reference, so
shared references and reference cycles are part of the compared value.

Measured on native Linux x86_64, CPython 3.12.13 → 3.13.14, at commit
`40cc9dd` (Actions run 30658976309). Raw result:
`compatibility/results/cross-python-3.12.13-to-3.13.14-linux-x86_64-2026-07-31.json`.

| Classification | Cases |
| --- | ---: |
| Accepted and correct | 189 |
| Explicitly refused | 0 |
| Unsupported by the language frontend | 8 |
| Unsupported live object at checkpoint | 6 |
| Program runtime failure | 1 |
| Infrastructure failure | 0 |
| **Silent mismatch** | **0** |
| Total | 204 |

Correctness among accepted cases: **100%**. Refusals and the three
out-of-scope classifications are reported separately and are not folded into
that rate.

The three out-of-scope reasons are counted apart because they say different
things about the runtime, and reporting all of them as a frontend gap
overstated the frontend's share:

- 8 cases are 8 corpus programs the compiler does not accept at all
  (`assignment_chained`, `class_accumulator`, `comparison_chained`,
  `comprehension_dict`, `comprehension_list`, `comprehension_set`,
  `fstring_conversion`, `simulation_inventory`). That is a language-coverage
  gap, not a portability result.
- 6 cases are `hash_sha256_chunks`, which compiles and runs but holds a live
  `_hashlib.HASH` at the checkpoint. That is a checkpoint-object limit.
- 1 case is `iteration_dictionary`, where the program itself raises while
  checkpointing iteration over `dict_items`. That is a runtime failure.

Live frame depth up to 16 was exercised, across 11 distinct frame chains and
40 programs.

A suite reporting zero mismatches is only meaningful if it can detect one, so
`tests/test_cross_python_differential.py` corrupts each compared dimension in
turn — including replaying a completed action and restarting from program
entry — and asserts every corruption is caught.

## Method

The initial corpus contains 50 unchanged, self-contained, MIT-licensed
pure-Python programs under `compatibility/programs/`. They cover text and JSON
processing, recursion, graph traversal, sorting, aggregation, hashing,
deterministic random simulation, dynamic programming, nested control flow, and
representative unsupported syntax.

For every program, `python3 -m compatibility.runner` performs:

1. ordinary CPython execution;
2. uninterrupted Continuum execution;
3. Continuum checkpoint and restore in the harness process;
4. Continuum checkpoint and restore in a newly created Python process.

The exact source file is used in every mode. Text-mode host line endings are
decoded and output is then compared as canonical UTF-8 bytes; this prevents
Windows CRLF from being misclassified as a language-semantic difference.
Failures remain in the denominator. Cross-platform status remains `not_run`
until these exact programs are exercised by the native proof workflow.

The runner is host-independent and the line-ending handling above lets it run
unchanged on Windows, but the published report below has only ever been
generated on Linux x86_64. The test suite runs two corpus programs through all
four gates on every host, which is a smoke check of the runner, not a
compatibility measurement for that host.

## Measured results

The first two columns were measured on 2026-07-29 with CPython 3.12.13 on
Linux x86_64. The IR 0.4 column was measured on 2026-07-30 with the same
CPython on Windows x86_64, so it is also the first corpus run on a host other
than Linux. Raw results:
`compatibility/results/ir-0.4-runtime-0.3.0-windows-x86_64-2026-07-30.json`.

| Gate | Initial IR 0.2 | IR 0.3 | IR 0.4 / runtime 0.3.0 |
| --- | ---: | ---: | ---: |
| Corpus total | 50 | 50 | 50 |
| Compiled | 34 | 37 | 42 |
| Uninterrupted output matched CPython | 33 | 36 | 41 |
| Same-process checkpoint/resume matched CPython | 32 | 35 | 40 |
| New-process checkpoint/resume matched CPython | 32 | 35 | 40 |
| Full compatibility rate | **64.0%** | **70.0%** | **80.0%** |
| Cross-platform corpus validation | not run | not run | not run |
| Corpus regenerated on macOS arm64 | not run | not run | not run |
| Corpus regenerated on Windows x86_64 | not run | not run | measured, this column |

Raw per-program results and timings are in
`compatibility/results/baseline-2026-07-29.json`; the generated table is in
`compatibility/results/baseline-2026-07-29.md`. The post-feature equivalents
are `after-defaults-2026-07-29.json` and `after-defaults-2026-07-29.md`.

Corpus timing fields are diagnostic wall times. CPython is launched as a
separate process while the uninterrupted VM timing is measured in-process, so
the corpus does not calculate or publish a slowdown ratio. The controlled
benchmark in `PERFORMANCE.md` remains the performance source.

## Current retained failures

- 8 programs fail compilation with explicit diagnostics at IR 0.4: three
  comprehensions, two augmented assignments to an attribute or subscript,
  one chained assignment, one chained comparison, and one f-string
  conversion.
- One program uses a live `_hashlib.HASH` value at the selected checkpoint;
  image creation rejects that native object.
- One program iterates a `dict_items` view; the runtime rejects that iterator
  instead of silently restoring it incorrectly.

The broader “only positional parameters” diagnostic appears three times but
covers three separate features: keyword-only arguments, `*args`, and
`**kwargs`. Comprehensions also account for three programs but require correct
comprehension scoping rather than only an opcode addition.

## Feature priority

1. Keyword-only and variadic calling conventions: three separate semantic
   features behind one diagnostic.
2. Comprehensions: three programs, but require a correct comprehension scope
   and nested execution representation.
3. Portable dictionary-view iteration: one runtime failure.
4. Portable live hash state: one checkpoint failure; likely requires a
   deliberate portable hashing model rather than serializing native state.
5. Chained comparisons and augmented subscript/attribute assignment: one
   program each, with evaluation-order tests required before support.

No failure is removed from the corpus after a feature is implemented. Results
must be regenerated so compatibility changes remain measurable.
