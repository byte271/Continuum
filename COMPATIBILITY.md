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

The exact source file is used in every mode. Stdout is compared byte for byte.
Failures remain in the denominator. Cross-platform status remains `not_run`
until these exact programs are exercised by the native proof workflow.

## Initial result

Measured on 2026-07-29 with CPython 3.12.13:

| Gate | Programs |
| --- | ---: |
| Corpus total | 50 |
| Compiled | 34 |
| Uninterrupted output matched CPython | 33 |
| Same-process checkpoint/resume matched CPython | 32 |
| New-process checkpoint/resume matched CPython | 32 |
| Full compatibility rate | **64.0%** |
| Cross-platform corpus validation | not run |

Raw per-program results and timings are in
`compatibility/results/baseline-2026-07-29.json`; the generated table is in
`compatibility/results/baseline-2026-07-29.md`.

Corpus timing fields are diagnostic wall times. CPython is launched as a
separate process while the uninterrupted VM timing is measured in-process, so
the corpus does not calculate or publish a slowdown ratio. The controlled
benchmark in `PERFORMANCE.md` remains the performance source.

## Retained failures

- 16 programs fail compilation with explicit diagnostics.
- One program uses a live `_hashlib.HASH` value at the selected checkpoint;
  image creation rejects that native object.
- One program iterates a `dict_items` view; the runtime rejects that iterator
  instead of silently restoring it incorrectly.

The most frequent coherent compile failure is default function arguments:
three programs. The broader “only positional parameters” diagnostic also
appears three times but covers three separate features: keyword-only
arguments, `*args`, and `**kwargs`.

## Feature priority

1. Default function arguments: three corpus programs; representation is
   definition-time portable values plus existing call binding.
2. Keyword-only and variadic calling conventions: three separate semantic
   features behind one diagnostic.
3. Comprehensions: three programs, but require a correct comprehension scope
   and nested execution representation.
4. Portable dictionary-view iteration: one runtime failure.
5. Portable live hash state: one checkpoint failure; likely requires a
   deliberate portable hashing model rather than serializing native state.

No failure is removed from the corpus after a feature is implemented. Results
must be regenerated so compatibility changes remain measurable.
