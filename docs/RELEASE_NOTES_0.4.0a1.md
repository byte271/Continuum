# 0.4.0a1 — cross-Python Execution ABI

Alpha. The image format and IR are still unstable. Nothing here changes what
Continuum can execute; it changes what can restore a Continuum image.

## The headline

A program frozen under **CPython 3.12.13** on native Linux x86_64 can be
verified and resumed under **CPython 3.13.14** on native Apple Silicon macOS
arm64, through the ordinary public CLI, after the source process has exited.

Verified by [Actions run 30658976309](https://github.com/byte271/Continuum/actions/runs/30658976309)
at commit `40cc9dd`:

| Property | Result |
| --- | --- |
| Source | Linux x86_64, CPython 3.12.13 |
| Target | macOS arm64, CPython 3.13.14 (native, Rosetta refused) |
| Source process exited and reaped before target read the image | yes |
| Image SHA-256 at capture / arrival / after restore | `3b564d9d…39824` / identical / identical |
| Live logical frames restored | 4 |
| Completed actions repeated | 0 |
| Source + target output vs. independent uninterrupted control | identical |
| Commands used | `continuum run`, `freeze`, `verify`, `resume` only |

## Why this is possible

Continuum's live execution state is held in its own virtual machine — explicit
frames, logical program counters, operand stacks, lexical cells, and control
blocks — not in CPython frame objects. The target restores the IR stored in the
image rather than recompiling the program, so the creator interpreter's AST and
bytecode details never enter the restore path.

The previous release nonetheless refused such a restore, because compatibility
was expressed as two fields: the exact creator Python version and the exact
creator Continuum version. Both are provenance. Neither answers whether a target
can reconstruct the state.

## The execution compatibility contract

Container format **0.2** carries an `execution_contract` block that separates
the axes 0.1 collapsed:

- container format version
- graph codec version
- Continuum IR version
- execution ABI version
- creator Continuum version *(provenance)*
- creator Python version *(provenance)*
- accepted target runtime implementations
- explicitly verified target Python versions
- required named capabilities

A target may restore only when it implements the exact execution ABI and every
required capability. The interpreter decision has two independent gates: the
running interpreter must appear in the image's allowlist **and** in this
runtime's own verified list. An image therefore cannot widen what this runtime
accepts by asserting a version nobody proved.

The allowlist is exact and never a range. `3.13.0` and `3.12.14` are refused as
firmly as `3.9`.

Every refusal carries a stable machine-readable reason code.

## Behavior changes

- **Creator Continuum version is no longer a restore requirement** for format
  0.2 images. The execution ABI is.
- `continuum run`, `verify`, and `resume` now work on any verified interpreter.
  Previously a single hard-coded version check refused everything else, which is
  why cross-Python restore had to be demonstrated outside the CLI.
- `inspect`, `verify`, `resume`, and `doctor` report the contract axes. `resume`
  names both interpreters, so a cross-Python restore is visible to an operator.
- `requires-python` widened to `>=3.12.13,<3.14`. This is an install-time filter
  only; the exact runtime allowlist is the authority, and a version the
  specifier admits but CI never proved is still refused. Both halves are tested.
- Container format bumped 0.1 → 0.2. **Format 0.1 images remain readable** and
  keep their original exact-Python, exact-runtime rule, with refusal messages
  that name the format version and explain how to obtain cross-Python restore.

## Evidence

- Native cross-Python public-CLI proof: run 30658976309 (3 jobs green).
- Cross-Python differential corpus, 3.12.13 → 3.13.14: 204 cases over 50
  programs — 189 accepted and correct, **0 silent mismatches**, 0 infrastructure
  failures, live frame depth to 16, 11 distinct frame chains.
  Correctness among accepted cases: **100%**. The 15 remaining cases are
  reported separately and excluded from that rate. Reclassified after 0.4.0a1
  shipped, when review found all three reasons were being labelled as a
  frontend gap: 8 are programs the language frontend does not compile, 6 hold
  an unsupported live object at the checkpoint, and 1 is a program runtime
  failure. The accepted count and the zero-mismatch result are unchanged.
  Raw: `compatibility/results/cross-python-3.12.13-to-3.13.14-linux-x86_64-2026-07-31.json`.
- Full suite: 302 tests, green on CPython 3.12.13 and 3.13.14.
- The differential comparison is itself fault-injected: each compared dimension
  is corrupted in turn and the corruption asserted to be caught, so "zero
  mismatches" is a statement about Continuum rather than about a blind
  comparison.

## Not claimed

- arbitrary Python versions — only 3.12.13 and 3.13.14, exactly
- arbitrary process migration
- native CPython frame migration
- arbitrary hot reload or source changes (that is Phase 2, unreleased)
- thread, socket, subprocess, or native-extension-state migration
- any verified cross-platform path involving Windows, in either direction
- cross-Python restore on any platform pair other than
  Linux x86_64 → macOS arm64; other pairs are format-compatible but unproven
