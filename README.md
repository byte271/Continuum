# Continuum

[![Cross-platform proof](https://github.com/byte271/Continuum/actions/workflows/cross-platform-proof.yml/badge.svg)](https://github.com/byte271/Continuum/actions/workflows/cross-platform-proof.yml)
[![Cross-Python CLI proof](https://github.com/byte271/Continuum/actions/workflows/cross-python-cli-proof.yml/badge.svg)](https://github.com/byte271/Continuum/actions/workflows/cross-python-cli-proof.yml)
[![Runtime bundles](https://github.com/byte271/Continuum/actions/workflows/runtime-bundles.yml/badge.svg)](https://github.com/byte271/Continuum/actions/workflows/runtime-bundles.yml)
[![Version](https://img.shields.io/badge/version-0.4.0a1-blue.svg)](STATUS.md)
[![Python](https://img.shields.io/badge/CPython-3.12.13%20%7C%203.13.14-3776ab.svg)](#requirements)
[![Platforms](https://img.shields.io/badge/native-Linux%20%7C%20macOS%20%7C%20Windows-success.svg)](#platform-support)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Freeze a running Python program. Resume the same live execution state in a
new process.**

Four active frames, their locals, logical program counters, a partially
evaluated operand stack, `try/finally` control state, shared references, a
reference cycle, RNG state, and an open file at a nonzero offset all survive
the move. Nothing replays. Nothing restarts. The program contains no
checkpoint calls at all.

It has already done this **between machines**: a program paused on native
Linux x86_64 resumed on a native Apple Silicon macOS arm64 runner, in a new
process, after the source process had exited — and produced output matching an
uninterrupted control run byte for byte.

Continuum compiles a controlled pure-Python subset into a portable
explicit-stack runtime. It does not migrate arbitrary CPython processes or
native machine state.

## Quickstart

```bash
python3 -m continuum doctor
python3 -m continuum demo --output-dir /tmp/continuum-demo
```

```powershell
python -m continuum doctor
python -m continuum demo --output-dir $env:TEMP\continuum-demo
```

```console
Same-machine continuation demonstration
Source held at safe point 16000 awaiting a published freeze request
Freeze request published; releasing the held safe point
Checkpoint committed
Source process exited
Original bundled input deleted
Continuation restored
Last source progress: Processing 2.0%
First resumed progress: Processing 4.0%
Combined output matches uninterrupted control: yes
Final result hash matches control: yes
```

## Platform support

Continuum runs natively on three platforms. Every push builds exact CPython
3.12.13 **from source** on each one and runs the complete test suite, the
transactional installer, and `continuum doctor` against the moved bundle.

| Platform | Runs natively | Same-host continuation | Image moves to another platform |
| --- | :---: | :---: | --- |
| Linux x86_64 | ✅ | ✅ CI-verified | ✅ verified → macOS arm64 |
| Apple Silicon macOS arm64 | ✅ | ✅ CI-verified | ✅ verified ← Linux x86_64 |
| Windows x86_64 | ✅ | ✅ CI-verified | ⚪ never run |
| Windows arm64 | ❌ | ❌ | ❌ rejected by the image format |

**Same-host continuation** means checkpoint, source-process exit, and resume in
a new process on the same machine. The Windows job additionally runs a complete
`continuum demo` continuation against its moved bundle.

**Cross-platform continuation** is a separate, stronger claim, and it holds for
exactly one direction: native Linux x86_64 to native Apple Silicon macOS arm64.
No cross-platform path involving Windows has been run in either direction, so
none is claimed. Running natively on a platform is not evidence that an image
moves to or from it — see [PORTABILITY.md](PORTABILITY.md) for the tested pairs
and [ROADMAP.md](ROADMAP.md) for the Windows proof leg that would change this.

The published 50-program compatibility corpus report is a Linux x86_64
measurement and has not been regenerated on Windows or macOS.

## Verified cross-platform proof

- Run: [30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
- Commit: [`15bceefece050d06a1f504244a77434e31fd5228`](https://github.com/byte271/Continuum/commit/15bceefece050d06a1f504244a77434e31fd5228)
- Source: native x86_64 Linux GitHub-hosted VM
- Target: native Apple Silicon macOS arm64 GitHub-hosted runner
- Runtime: CPython 3.12.13, built independently from the same verified source
- Conditions: 26/26 passed
- Image SHA-256: `5a72261f61a3df2b71aec6882d3dbfc31196813a1bbfa5438cd9e9d069f324b9`

The source process exited and was reaped before the target job started. The
target resumed the unchanged image in a new process at iteration 65 after the
source stopped at iteration 64. Four active frames, locals, logical PCs, an
operand item, control state, shared references, a cycle, RNG state, and a
bundled file at a nonzero offset survived. The 6,605 combined output lines and
final result hash matched an uninterrupted control run byte for byte, with no
repeated completed action. See [PORTABILITY.md](PORTABILITY.md) and the
[validation protocol](validation/cross_platform/README.md).

The verified scope remains narrow: CPython 3.12.13 and 3.13.14 exactly, one
thread, a controlled language subset, Continuum safe points, and read-only
regular files. Classes, closures, generators, native extensions, subprocesses,
sockets, writable files, and arbitrary CPython frames are unsupported.

### It has also crossed Python versions

At commit
[`40cc9dd`](https://github.com/byte271/Continuum/commit/40cc9dd0ed1b2a81dfd265665c1232363d496dc8)
([Actions run 30658976309](https://github.com/byte271/Continuum/actions/runs/30658976309)),
a program was started and frozen on native Linux x86_64 under **CPython
3.12.13**, the source process exited and was reaped, and the unchanged image was
verified and resumed on a native Apple Silicon macOS arm64 runner under
**CPython 3.13.14**.

The whole path used only the public CLI — `continuum run`, `continuum freeze`,
`continuum verify`, `continuum resume`. The image SHA-256 was
`3b564d9d37a9353ebb22027a4b3597d30fc2eef1272c3a220fc7f65e3d939824` at capture,
on arrival, and after restore. Four live logical frames were restored, zero
completed actions repeated, and source-plus-target output equalled an
independently run uninterrupted control.

This works because Continuum's execution state lives in its own VM — explicit
frames, logical program counters, operand stacks, and lexical cells — rather
than in CPython frame objects, and because the target restores the IR stored in
the image instead of recompiling the source. The restore is authorized by an
explicit execution ABI plus an exact interpreter allowlist, not by matching the
creator's interpreter. What is **not** claimed: arbitrary Python versions,
arbitrary process migration, or native CPython frame migration.

That proof is immutable evidence for Continuum IR 0.2 at the commit above.

The same two-job workflow was rerun for IR 0.3 at runtime `0.2.0a1`: its
`linux-source` and dependent `macos-target` jobs passed at commit
[`3a4a43fb74331113225d7b9a3a0fef4afd1371fa`](https://github.com/byte271/Continuum/commit/3a4a43fb74331113225d7b9a3a0fef4afd1371fa)
in [Actions run 30509186641](https://github.com/byte271/Continuum/actions/runs/30509186641).

A runtime version is part of the image compatibility contract, so the `0.2.0`
release reran that proof rather than inheriting it. Both jobs passed again at
the release commit `a73073d` in
[Actions run 30585208329](https://github.com/byte271/Continuum/actions/runs/30585208329),
with a freshly generated `0.2.0` image. The workflow runs on every push to
`main`; the badge at the top of this file reports the current state of that
one direction.

## Which CI job proves what

| Workflow | Jobs | What it establishes |
| --- | --- | --- |
| [`runtime-bundles.yml`](.github/workflows/runtime-bundles.yml) | `linux-x86_64` (`ubuntu-24.04`), `macos-arm64` (`macos-26`), `windows-x86_64` (`windows-2025`) | Native build from CPython source, complete suite, installer, and `doctor` on each platform — same-host continuation only |
| [`cross-platform-proof.yml`](.github/workflows/cross-platform-proof.yml) | `linux-source` → `macos-target` | One image written on Linux x86_64 and resumed on macOS arm64, with independent evidence verification |

There is no third job pairing Windows with another platform, which is exactly
why no such claim appears anywhere in this repository.

## Why this is a real continuation

`continuum run` does not execute the program on the ordinary CPython frame
stack. It parses the source and lowers the supported subset into Continuum IR.
The runtime owns explicit frames:

```text
Frame(function_id, pc, locals, operand_stack, control_blocks, finally_reasons)
```

At each `SAFEPOINT`, the current instruction has already committed and `pc`
points at the next instruction. The image stores those frames and the object
graph they reference. `continuum resume` loads `code/ir.json`; it does not call
the program entry point, replay earlier statements, or invoke the original
functions with saved arguments.

This is a language-level execution layer, not arbitrary CPython process
migration. Programs outside the supported subset are rejected before running
or at checkpoint time.

The exact feature matrix is [LANGUAGE_SUPPORT.md](LANGUAGE_SUPPORT.md), and
the stage-by-stage hostile audit is [AUDIT.md](AUDIT.md).

## Requirements

- CPython 3.12.13 or 3.13.14, exactly — these are the versions verified end to
  end by native CI;
- Linux x86_64, Apple Silicon macOS arm64, or Windows x86_64;
- one thread;
- only the standard library is needed.

The allowlist is exact rather than a range, and it is enforced at runtime, not
just at install time. `3.13.0` and `3.12.14` are refused as firmly as `3.9`,
because neither has been proven. Adding a version requires a green cross-Python
proof run, not a version bump. See [COMPATIBILITY.md](COMPATIBILITY.md).

## Run

From a checkout of the repository:

```bash
python3 -m continuum doctor
python3 -m continuum demo --output-dir /tmp/continuum-demo
```

On Windows, use `python` and a Windows path:

```powershell
python -m continuum doctor
python -m continuum demo --output-dir $env:TEMP\continuum-demo
```

An installed Windows bundle exposes the same CLI as `continuum.cmd`.

The demo automatically launches an unchanged nested-call workload, freezes a
live continuation, deletes the original bundled input, resumes in a new
process, runs an uninterrupted control, and retains its comparison evidence.
It is explicitly a same-machine demonstration.

The demonstration is deterministic on any host speed. Its source process is
held at one safe point, mid-workload, until the real `continuum freeze`
command has published its request, and is only then released to observe that
request. The freeze protocol itself is unchanged: this synchronization is
confined to the `demo` harness and never affects `continuum run`. The retained
`comparison.json` records the hold point and that the request existed while
the source was still alive.

To operate the CLI manually:

```bash
python3 -m continuum run --file-policy bundle \
  examples/demo.py examples/demo_input.txt 200000
```

The first stderr line contains a session ID:

```text
Continuum session: cont-0123456789ab
```

From another terminal:

```bash
python3 -m continuum sessions
python3 -m continuum freeze cont-0123456789ab -o process.cont
python3 -m continuum inspect process.cont
python3 -m continuum verify process.cont
python3 -m continuum resume process.cont
```

A freeze request is published atomically as a control file. On POSIX hosts the
running session is then notified with `SIGUSR1`, so idle safe points touch no
filesystem. Windows has no `SIGUSR1`: the session instead polls for the request
file at safe points, at most once every 10 ms. The freeze protocol and image
are identical; only the notification path and its idle cost differ.

`inspect` validates the container and prints metadata. `verify` additionally
decodes the allowlisted graph and reconstructs frame state without opening
resources, compiling bundled source, or starting execution. Neither command
makes an untrusted image safe to resume.

`bundle` puts read-only input-file bytes in the image. The default `strict`
policy instead requires the same absolute path, content hash, size, and mtime
on resume. `relocate` maps a strict resource to another path:

```bash
python3 -m continuum resume process.cont \
  --file-policy relocate \
  --relocate /linux/path/input.txt=/Users/me/input.txt
```

Relocation still verifies size and SHA-256. It never silently accepts a
different file. `OLD` must be an absolute POSIX or Windows path exactly as the
source host recorded it; `NEW` is resolved on the current host.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite discovers 369 tests and is run natively on Linux x86_64, Apple
Silicon macOS arm64, and Windows x86_64 by `runtime-bundles.yml`. Tests whose
mechanism does not exist on the current host skip explicitly: POSIX signal
notification and the shell installer skip on Windows, and the native Apple
Silicon test skips everywhere else.

The adversarial process test routes source and target actions through a
separate fsyncing auditor, waits for the source PID to exit, starts a new
target process, rejects duplicated action nonces, and compares the final hash
with an uninterrupted control. The Apple Silicon test is present but skips
unless it receives an image produced by qualified native Linux x86_64:

```bash
CONTINUUM_LINUX_IMAGE=/path/to/linux-image.cont \
  python3 -m unittest tests.test_cross_platform_manual -v
```

See [docs/TESTING.md](docs/TESTING.md) for the claim-to-test matrix.
The stricter two-machine evidence workflow is in
[validation/cross_platform/README.md](validation/cross_platform/README.md).
The unchanged 50-program differential corpus and both measured reports are
described in [COMPATIBILITY.md](COMPATIBILITY.md).

## Installation status

The repository contains self-contained bundle builders and bootstrap
installers under `packaging/`. Linux x86_64 and macOS arm64 build
`.tar.gz` archives through `build_bundle.sh` and install with `install.sh`;
Windows x86_64 builds a `.zip` through `build_bundle_windows.ps1` and installs
with `install.ps1`.

All three bundles are built, moved, extracted, exercised with the complete
test suite, installed into a fresh prefix, and checked with `continuum doctor`
on every push to `main` by `runtime-bundles.yml`.

The **v0.3.0** release publishes all three, each built by the CI run above
and verified against its recorded digest before upload:

| Platform | Asset | SHA-256 |
| --- | --- | --- |
| Linux x86_64 | [`continuum-linux-x86_64.tar.gz`](https://github.com/byte271/Continuum/releases/download/v0.3.0/continuum-linux-x86_64.tar.gz) | `8cd80c2d0094be1331107f2b8762085271112c4655dc853d4050cfaa9d3ec9f1` |
| Apple Silicon macOS arm64 | [`continuum-macos-arm64.tar.gz`](https://github.com/byte271/Continuum/releases/download/v0.3.0/continuum-macos-arm64.tar.gz) | `1237449dff8d5d92db39ad36e156455784e08156e040348559b0609e90a1f009` |
| Windows x86_64 | [`continuum-windows-x86_64.zip`](https://github.com/byte271/Continuum/releases/download/v0.3.0/continuum-windows-x86_64.zip) | `05c41f9b50858c400cadd6ad52b051e02a91ba22f8fac6d70df7c9a4eee1b0e0` |

```bash
curl -LO https://github.com/byte271/Continuum/releases/download/v0.3.0/continuum-linux-x86_64.tar.gz
packaging/install.sh --archive continuum-linux-x86_64.tar.gz --sha256 8cd80c2d0094be1331107f2b8762085271112c4655dc853d4050cfaa9d3ec9f1
```

```powershell
.\packaging\install.ps1 -Archive continuum-windows-x86_64.zip -Sha256 05c41f9b50858c400cadd6ad52b051e02a91ba22f8fac6d70df7c9a4eee1b0e0
```

The installer requires the expected digest and refuses to proceed without it;
`install.ps1` and `install.sh` both accept an HTTPS archive directly and refuse
plain HTTP.

The Windows path was exercised end to end from the published URL above:
downloaded, digest-verified, installed into a fresh prefix, checked with
`continuum doctor`, and used to run a complete `continuum demo` continuation.
The Linux and macOS archives are published with the same shape but their
network install has only been exercised in CI against a local archive.

## Repository map

- `continuum/compiler.py` — Python AST to Continuum IR compiler.
- `continuum/vm.py` — explicit frame and operand-stack interpreter.
- `continuum/codec.py` — allowlisted, identity-preserving graph codec.
- `continuum/resources.py` — strict, relocate, and bundle file rebinding.
- `continuum/image.py` — atomic `.cont` writer, reader, and integrity checks.
- `continuum/session.py` — session records and atomic freeze control files.
- `packaging/` — relocatable exact-runtime bundles and transactional
  installers; `build_bundle.sh`/`install.sh` for POSIX hosts and
  `build_bundle_windows.ps1`/`install.ps1` for Windows.
- `validation/windows/build_cpython.ps1` — native Windows exact-CPython build.
- `compatibility/` — unchanged 50-program CPython differential corpus.
- `docs/adr/0001-portable-explicit-stack-vm.md` — architecture decision.
- `FORMAT.md`, `SECURITY.md`, `LIMITATIONS.md`, `STATUS.md` — exact contracts.
- `PERFORMANCE.md` — measured, reproducible prototype costs.
- `ROADMAP.md` — concrete release-gated next milestones.
- `LANGUAGE_SUPPORT.md`, `PORTABILITY.md`, `AUDIT.md` — tested subset,
  encoding/host assumptions, and confirmed defects.

## License

MIT. See [LICENSE](LICENSE).
