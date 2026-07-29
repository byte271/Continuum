# Status

Updated: 2026-07-29

## WORKING

- CPython 3.12.13 source-subset compilation into Continuum IR 0.2.
- Explicit frames, next logical PCs, locals, operand stacks, module globals,
  and supported `try/finally` control state.
- Same-host Linux x86_64 checkpoint, atomic image commit, source-process exit,
  and resume in a new process without source replay.
- External-auditor anti-restart proof for entry execution, three function
  prologues, completed loop actions, ordered source-exit/target-start process
  events, and final control hash.
- Partial caller operand-stack restoration and freeze between iterator advance
  and loop-body execution.
- Shared references, supported mutable cycles, deterministic set encoding,
  module RNG state, and `random.Random` state.
- Read-only regular files with strict, relocate, and bundle policies;
  multiple offsets, relocation, and deletion of bundled originals are tested.
- Bounded ZIP reader, complete entry checksums, cross-document identity checks,
  required-capability negotiation, explicit graph codec, and malformed-image
  rejection.
- Failed checkpoint leaves source state live and permits a later successful
  retry.
- Public `run`, `sessions`, `freeze`, `inspect`, `resume`, and `--version`.
- Current full suite: 59 tests discovered, 58 passed, one native Apple Silicon
  test skipped on this Linux x86_64 host.

## PARTIALLY WORKING

- Python semantics are limited to `LANGUAGE_SUPPORT.md`.
- `try/finally` preserves normal/exceptional pending reasons, but control
  transfers out of `try`, handlers, causes, and tracebacks are unsupported.
- Safe points are statement/back-edge/finally boundaries, not every IR opcode.
- Module globals and RNG state survive; arbitrary mutable imported-module
  state does not.
- Dictionary iteration detects changed key sequences but not every CPython
  version-tag mutation.
- Atomic image replacement and file fsync work; directory fsync failure is
  ignored for compatibility.
- Failure injection covers request publication, unsupported-state preflight,
  retry, corruption, truncation, and compatibility—not every commit stage.
- Performance remains about 150× slower than the native control without
  session polling on the audited workload.

## NOT WORKING

- Arbitrary CPython frames, arbitrary PIDs, native instruction boundaries, or
  arbitrary Python programs.
- Closures, classes/instances, generators, context managers, comprehensions,
  `try/except`, default/variadic parameters, and other rejected syntax.
- Native extensions with live state, NumPy/PyTorch execution, GPU memory,
  threads, subprocesses, sockets, locks, devices, and writable files.
- Image signatures, authentication policy, encryption, secret redaction, or a
  security sandbox.
- Python versions other than 3.12.13.

## UNVERIFIED ON REAL HARDWARE

- Linux x86_64 to Apple Silicon macOS arm64 continuation.
- Any cross-architecture or cross-operating-system continuation.
- Resume in a second native Linux x86_64 environment or after an actual reboot.
- Native macOS resource behavior and directory durability.
- The prepared package in `validation/cross_platform/` has passed its Linux
  source and same-host control dry run only. No target evidence was generated.
- `.github/workflows/cross-platform-proof.yml` now defines the required
  `ubuntu-24.04` source and dependent `macos-26` target jobs, but it has not
  been dispatched because this checkout has no GitHub remote or authenticated
  GitHub CLI. No workflow URL, run ID, or Actions artifact exists yet.
