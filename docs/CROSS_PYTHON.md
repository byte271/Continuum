# Cross-Python execution image experiment

Status: experimental branch only. No cross-Python compatibility claim is made until
`cross-python-proof.yml` passes end to end and retains its evidence.

## Target capability

One unchanged Continuum image must cross all three boundaries in one proof:

- Linux x86_64 to native Apple Silicon macOS arm64;
- CPython 3.12.13 to CPython 3.13.14;
- source process/job completion before target restoration.

The live logical frame chain, locals, closure cells, VM-owned instance state, shared
references, a reference cycle, and the next logical instruction must survive. Completed
observable actions must not repeat. Source-plus-target output and the final result must
match an uninterrupted target-runtime control.

## Compatibility model under test

Shipping images currently require an exact CPython patch version and an exact Continuum
runtime version. The experiment adds an explicit execution ABI:

```text
continuum-execution-abi-1.0
```

The creator runtime version remains provenance. Restore acceptance is based on:

1. image format and IR validation;
2. the exact execution ABI;
3. mandatory capability support;
4. an explicit allowlist of verified Python versions;
5. the existing OS/architecture target list.

This is not a promise that all Python 3.12 or 3.13 patches are equivalent. Only the exact
versions exercised by retained evidence are accepted.

## Refusal rules

The portable reader refuses rather than guesses when:

- the execution ABI differs or is absent;
- the current Python patch is not in the image allowlist;
- the IR capability is unknown;
- creator metadata is internally inconsistent;
- an existing image, graph, frame, resource, checksum, or platform invariant fails.

The ordinary exact-version image reader intentionally rejects the experimental ABI
capability. The experiment does not silently weaken the shipping restore path.

## Release gate

A future runtime release may promote this path only after all of these are true:

- the workflow passes from a clean, exact commit;
- source and transferred image SHA-256 values are identical;
- the source job finishes before the dependent target job begins;
- CPython versions are exactly 3.12.13 and 3.13.14;
- source is native Linux x86_64 and target is native macOS arm64 without Rosetta;
- four active logical frames are restored;
- shared-reference and cycle identity survive;
- every action appears exactly once;
- combined output and result match an uninterrupted control;
- unsupported Python versions and ABI mismatches are covered by rejection tests;
- no existing exact-version image behavior regresses.

Until that gate is met, this work is a feasibility experiment, not a published platform
claim.
