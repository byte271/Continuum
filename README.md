# Continuum

[![Cross-platform proof](https://github.com/byte271/Continuum/actions/workflows/cross-platform-proof.yml/badge.svg)](https://github.com/byte271/Continuum/actions/runs/30489463484)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Continuum pauses a supported Python program on Linux x86_64 and resumes the
same live execution state in a new process on Apple Silicon macOS arm64.

Continuum currently compiles a controlled pure-Python subset into a portable
explicit-stack runtime. It does not migrate arbitrary CPython processes or
native machine state.

```console
# Linux x86_64
$ python3 -m continuum run --file-policy bundle examples/demo.py examples/demo_input.txt 200000
Continuum session: cont-a84c9f31b012
$ python3 -m continuum freeze cont-a84c9f31b012 -o process.cont

# Copy process.cont unchanged to Apple Silicon macOS
$ python3 -m continuum resume process.cont --file-policy bundle
Restored from Linux x86_64.
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
- Linux or macOS;
- one thread;
- only the standard library is needed.

The exact patch version is intentional while the image and IR are unstable.

## Run

From a checkout of the repository:

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
python3 -m continuum resume process.cont
```

`bundle` puts read-only input-file bytes in the image. The default `strict`
policy instead requires the same absolute path, content hash, size, and mtime
on resume. `relocate` maps a strict resource to another path:

```bash
python3 -m continuum resume process.cont \
  --file-policy relocate \
  --relocate /linux/path/input.txt=/Users/me/input.txt
```

Relocation still verifies size and SHA-256. It never silently accepts a
different file.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

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

## Repository map

- `continuum/compiler.py` — Python AST to Continuum IR compiler.
- `continuum/vm.py` — explicit frame and operand-stack interpreter.
- `continuum/codec.py` — allowlisted, identity-preserving graph codec.
- `continuum/resources.py` — strict, relocate, and bundle file rebinding.
- `continuum/image.py` — atomic `.cont` writer, reader, and integrity checks.
- `continuum/session.py` — session records and atomic freeze control files.
- `docs/adr/0001-portable-explicit-stack-vm.md` — architecture decision.
- `FORMAT.md`, `SECURITY.md`, `LIMITATIONS.md`, `STATUS.md` — exact contracts.
- `PERFORMANCE.md` — measured, reproducible prototype costs.
- `LANGUAGE_SUPPORT.md`, `PORTABILITY.md`, `AUDIT.md` — tested subset,
  encoding/host assumptions, and confirmed defects.

## License

MIT. See [LICENSE](LICENSE).
