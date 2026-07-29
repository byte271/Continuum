# Status

Updated: 2026-07-29

## WORKING

- Verified cross-platform continuation from a native x86_64 Linux
  GitHub-hosted VM to a native Apple Silicon macOS arm64 GitHub-hosted runner.
  Actions run
  [30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
  passed 26/26 proof conditions at commit
  `15bceefece050d06a1f504244a77434e31fd5228`.
- The verified source process exited and was reaped before a new target
  process resumed the unchanged image. Source/transfer/target image hashes
  were identical; source-plus-target output and the final result hash matched
  the uninterrupted control.
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
- Current full suite: 60 tests discovered, 59 passed, one native Apple Silicon
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

## UNVERIFIED

- Resume in a second native Linux x86_64 environment or after an actual reboot.
- Native macOS resource behavior and directory durability.
- Any source/target platform pair other than the verified GitHub-hosted Linux
  x86_64 to Apple Silicon macOS arm64 pair.
- Cross-platform restoration for programs or resources outside the exact
  verified controlled subset.
