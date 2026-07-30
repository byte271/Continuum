# Continuum

[![Cross-platform proof](https://github.com/byte271/Continuum/actions/workflows/cross-platform-proof.yml/badge.svg)](https://github.com/byte271/Continuum/actions/workflows/cross-platform-proof.yml)
[![Runtime bundles](https://github.com/byte271/Continuum/actions/workflows/runtime-bundles.yml/badge.svg)](https://github.com/byte271/Continuum/actions/workflows/runtime-bundles.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Continuum pauses a supported Python program on Linux x86_64 and resumes the
same live execution state in a new process on Apple Silicon macOS arm64.

Continuum runs natively on Linux x86_64, Apple Silicon macOS arm64, and
Windows x86_64. Continuation between two different platforms is verified for
exactly one direction: Linux x86_64 to Apple Silicon macOS arm64.

Continuum currently compiles a controlled pure-Python subset into a portable
explicit-stack runtime. It does not migrate arbitrary CPython processes or
native machine state.

```console
$ python3 -m continuum doctor
$ python3 -m continuum demo --output-dir /tmp/continuum-demo
Same-machine continuation demonstration
...
Combined output matches uninterrupted control: yes
```

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

The verified scope remains narrow: CPython 3.12.13 exactly, one thread, a
controlled language subset, Continuum safe points, and read-only regular
files. Classes, closures, generators, native extensions, subprocesses,
sockets, writable files, and arbitrary CPython frames are unsupported.

That proof is immutable evidence for Continuum IR 0.2 at the commit above.

The same two-job workflow has since been rerun for current IR 0.3 and runtime
0.2.0a1. Its `linux-source` and dependent `macos-target` jobs passed at commit
[`3a4a43fb74331113225d7b9a3a0fef4afd1371fa`](https://github.com/byte271/Continuum/commit/3a4a43fb74331113225d7b9a3a0fef4afd1371fa)
in [Actions run 30509186641](https://github.com/byte271/Continuum/actions/runs/30509186641).
The workflow runs on every push to `main`, so the badge above reports the
current state of that one direction.

Cross-platform evidence covers native Linux x86_64 to native Apple Silicon
macOS arm64 and nothing else. No cross-platform path involving Windows has
been run in either direction.

## Native platform support

[`runtime-bundles.yml`](.github/workflows/runtime-bundles.yml) builds exact
CPython 3.12.13 from source on each host, then runs the complete test suite,
the transactional installer, and `continuum doctor` against the moved bundle:

| Host | Runner | Same-host continuation |
| --- | --- | --- |
| Linux x86_64 | `ubuntu-24.04` | CI-verified |
| Apple Silicon macOS arm64 | `macos-26` | CI-verified |
| Windows x86_64 | `windows-2025` | CI-verified |

Same-host continuation means checkpoint, source-process exit, and resume in a
new process on the same machine. The Windows job additionally runs the
`continuum demo` continuation end to end against its moved bundle.

Windows arm64 is unsupported; images reject it. The published 50-program
compatibility corpus report is a Linux x86_64 measurement and has not been
regenerated on Windows or macOS. Native support on a host is not evidence that
an image moves between hosts; see [PORTABILITY.md](PORTABILITY.md) for the
tested pairs.

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

- CPython 3.12.13 exactly;
- Linux x86_64, Apple Silicon macOS arm64, or Windows x86_64;
- one thread;
- only the standard library is needed.

The exact patch version is intentional while the image and IR are unstable.

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

The suite discovers 90 tests and is run natively on Linux x86_64, Apple
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

No published release download or one-line network installer exists yet, so
this README does not publish an installer command. Bundles are currently
retained only as workflow artifacts.

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
