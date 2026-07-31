# Status

Version: 0.3.1 · IR 0.4 · image format 0.1 · CPython 3.12.13 exactly
Updated: 2026-07-30

## Platform matrix

| Platform | Native runtime | Same-host continuation | Cross-platform continuation |
| --- | :---: | :---: | --- |
| Linux x86_64 | ✅ | ✅ | ✅ to macOS arm64 |
| Apple Silicon macOS arm64 | ✅ | ✅ | ✅ from Linux x86_64 |
| Windows x86_64 | ✅ | ✅ | ⚪ never run, in either direction |
| Windows arm64 | ❌ | ❌ | ❌ not an accepted image target |

Native and same-host columns are established by `runtime-bundles.yml`; the
cross-platform column is established only by `cross-platform-proof.yml`, which
has no Windows job.

## WORKING

- Verified cross-platform continuation from a native x86_64 Linux
  GitHub-hosted VM to a native Apple Silicon macOS arm64 GitHub-hosted runner.
  Actions run
  [30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
  passed 26/26 proof conditions at commit
  `15bceefece050d06a1f504244a77434e31fd5228` for IR 0.2/runtime 0.1.1.dev0.
  The same two jobs passed for IR 0.3/runtime 0.2.0a1 in Actions run
  [30509186641](https://github.com/byte271/Continuum/actions/runs/30509186641)
  at commit `3a4a43fb74331113225d7b9a3a0fef4afd1371fa`. This is the only
  verified cross-platform direction. The 0.2.0 release reran both jobs at
  commit `a73073d` in Actions run
  [30585208329](https://github.com/byte271/Continuum/actions/runs/30585208329),
  and all three bundle jobs passed in run
  [30585208377](https://github.com/byte271/Continuum/actions/runs/30585208377).
- Native same-host continuation on Linux x86_64, Apple Silicon macOS arm64,
  and Windows x86_64. `runtime-bundles.yml` builds exact CPython 3.12.13 from
  source on each host and runs the complete suite, the installer, and
  `continuum doctor` against the moved bundle; the Windows job also runs a
  complete `continuum demo` continuation.
- The verified source process exited and was reaped before a new target
  process resumed the unchanged image. Source/transfer/target image hashes
  were identical; source-plus-target output and the final result hash matched
  the uninterrupted control.
- CPython 3.12.13 source-subset compilation into current Continuum IR 0.3.
- Explicit frames, next logical PCs, locals, operand stacks, module globals,
  and supported `try/finally` control state.
- Positional default arguments with definition-time evaluation, mutable
  identity across calls, keyword binding, and checkpoint restoration.
- Same-host checkpoint, atomic image commit, source-process exit, and resume
  in a new process without source replay, on each of the three native hosts.
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
- Signal-assisted freeze notification on POSIX hosts. Idle safe points read an
  in-memory Boolean; the request document is published atomically before
  `SIGUSR1`.
- Freeze notification on Windows, which has no `SIGUSR1`. The request document
  is published by the same atomic hard link, and safe points poll for it at
  most once every 10 ms. `continuum freeze` confirms the target process is
  alive through `OpenProcess`/`GetExitCodeProcess` instead of `kill(pid, 0)`.
  The protocol and the resulting image are unchanged; idle cost and freeze
  latency are not measured on Windows.
- Public `run`, `sessions`, `freeze`, `inspect`, `verify`, `resume`, `doctor`,
  `demo`, and `--version`.
- `verify` validates compatibility, decodes the allowlisted graph, and
  reconstructs frames without opening resources or starting execution.
- A 50-program unchanged-source differential corpus. Current IR 0.3 passes
  all four gates for 35 programs (70.0%), up from 32 (64.0%) before default
  arguments. That rate is a Linux x86_64 measurement; the suite exercises two
  corpus programs through all four gates on every host.
- Current full suite: 180 tests discovered. Tests skip only where the host
  lacks the mechanism under test: the native Apple Silicon test skips off
  macOS arm64, and POSIX signal notification, the shell installer, and the
  symlink launcher skip on Windows.

## IN PROGRESS

- Continuum IR 0.4, released as runtime 0.3.0. Milestone 1,
  portable `try/except`, is complete: handler matching, tuple matching, `as`
  binding with handler-exit unbinding, `else`, and `try/except/finally`, with
  the live exception carried as an ordinary portable operand so a checkpoint
  inside a handler serializes it like any other value. Milestone 2,
  complete argument binding, is complete: positional-only and keyword-only
  boundaries, `*args`, `**kwargs`, keyword-only defaults, and `*`/`**` call
  unpacking, with CPython's binding error messages reproduced exactly.
  Milestone 3, identity-preserving lexical closures, is complete: real
  shared cells, `nonlocal`, and multi-level capture, with two closures over
  one variable still sharing one binding after an image round trip.
  Milestone 4, VM-owned basic classes and instances, is complete: classes,
  methods, `__init__`, class and instance attributes, and attribute
  assignment, with no host type object or host instance created anywhere.
  Inheritance, descriptors, metaclasses, and user-defined exception classes
  remain out of this revision.
- The single combined Linux x86_64 to macOS arm64 proof for IR 0.4 passed in
  Actions run
  [30592158078](https://github.com/byte271/Continuum/actions/runs/30592158078)
  at commit `21f7b2e`, and again for the 0.3.0 release in run
  [30596179154](https://github.com/byte271/Continuum/actions/runs/30596179154)
  at commit `023f74c`. The migrated image carried a VM-owned class and
  instance, a live `try/except`, a variadic method binding, and a closure
  cell shared by two functions, all folded into the final digest.
- IR 0.4 supersedes the v0.2.0 release. An image written by either runtime
  is rejected by the other: the capability negotiation requires an exact
  `continuum-ir-<version>` match, so v0.2.0 images cannot be resumed by
  v0.3.0 and vice versa.
- IR 0.4 images are not interchangeable with IR 0.3 images: the runtime
  negotiates an exact `continuum-ir-<version>` capability, so v0.2.0 images
  are rejected by this revision and vice versa.
- No cross-platform proof has been run for IR 0.4. The single combined
  Linux x86_64 to macOS arm64 proof is planned after milestone 4.

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
- Performance remains about 159× slower than the native control without
  safe-point notification on the audited workload.
- Self-contained runtime bundles have builders and transactional installers
  for all three hosts: `.tar.gz` through `build_bundle.sh`/`install.sh` on
  POSIX, `.zip` through `build_bundle_windows.ps1`/`install.ps1` on Windows.
  All three are built, moved, installed, and checked in CI, and all three are
  published as v0.3.0 release assets with SHA-256 sidecars. The Windows
  archive was additionally installed from its published release URL and used
  to run a complete continuation; the Linux and macOS network installs have
  only been exercised in CI against a local archive.

## NOT WORKING

- Arbitrary CPython frames, arbitrary PIDs, native instruction boundaries, or
  arbitrary Python programs.
- Closures, classes/instances, generators, context managers, comprehensions,
  `try/except`, positional-only/keyword-only/variadic parameters, and other
  rejected syntax.
- Native extensions with live state, NumPy/PyTorch execution, GPU memory,
  threads, subprocesses, sockets, locks, devices, and writable files.
- Image signatures, authentication policy, encryption, secret redaction, or a
  security sandbox.
- Python versions other than 3.12.13.

## UNVERIFIED

- Any cross-platform path involving Windows, in either direction. The
  cross-platform proof workflow has exactly two jobs, `linux-source` and
  `macos-target`, and `validation/cross_platform/` contains no Windows source
  or target script. Windows images and Windows resumes have never been moved
  between hosts.
- Any source/target platform pair other than the verified GitHub-hosted Linux
  x86_64 to Apple Silicon macOS arm64 pair.
- Windows arm64, which is not an accepted image target pair at all.
- Resume in a second native Linux x86_64 environment or after an actual reboot.
- Native macOS and Windows resource behavior and directory durability.
- Windows idle safe-point and freeze-latency cost. `PERFORMANCE.md` contains
  Linux x86_64 measurements only.
- The 50-program corpus on Windows or macOS.
- Cross-platform restoration for programs or resources outside the exact
  verified controlled subset.
- Installing the Linux x86_64 and macOS arm64 archives from their published
  release URLs. Only the Windows path has been exercised over the network.
