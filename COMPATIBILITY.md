# Compatibility

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

## Measured results

Measured on 2026-07-29 with CPython 3.12.13:

| Gate | Initial IR 0.2 | After positional defaults, IR 0.3 |
| --- | ---: | ---: |
| Corpus total | 50 | 50 |
| Compiled | 34 | 37 |
| Uninterrupted output matched CPython | 33 | 36 |
| Same-process checkpoint/resume matched CPython | 32 | 35 |
| New-process checkpoint/resume matched CPython | 32 | 35 |
| Full compatibility rate | **64.0%** | **70.0%** |
| Cross-platform corpus validation | not run | not run |

Raw per-program results and timings are in
`compatibility/results/baseline-2026-07-29.json`; the generated table is in
`compatibility/results/baseline-2026-07-29.md`. The post-feature equivalents
are `after-defaults-2026-07-29.json` and `after-defaults-2026-07-29.md`.

Corpus timing fields are diagnostic wall times. CPython is launched as a
separate process while the uninterrupted VM timing is measured in-process, so
the corpus does not calculate or publish a slowdown ratio. The controlled
benchmark in `PERFORMANCE.md` remains the performance source.

## Current retained failures

- 13 programs fail compilation with explicit diagnostics.
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
