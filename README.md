# Continuum

Continuum saves the live execution state of supported Python programs and
restores it in a new runtime.

The current prototype is deliberately narrow. It automatically compiles a
pure-Python subset into a portable stack-machine IR. At a safe point it writes
the active Continuum frames, logical instruction positions, operand stacks,
locals, module globals, reachable heap graph, random state, and supported file
resources into a `.cont` image. Resume starts a new process and continues the
stored frames. It does not rerun the source file.

What has actually been verified:

- same-machine suspension and continuation on Linux x86_64;
- source-process exit followed by resume in a different process;
- four active nested frames with preserved locals and logical PCs;
- an external fsyncing auditor observed no repeated entry action, function
  prologue, or completed loop action after new-process resume;
- shared references, cycles, module globals, RNG state, and read-only file
  offsets;
- ZIP entry bounds, SHA-256 integrity checks, and incompatible-version
  rejection.
- malformed graph and resource records, altered IR identity, unknown mandatory
  capabilities, truncated archives, and failed-checkpoint retry.

Linux x86_64 to macOS ARM64 restoration has **not** been tested yet. The image
contains no Linux machine code or native pointers, but that is a design
property, not proof of a successful cross-platform run. See [STATUS.md](STATUS.md).

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

From the repository:

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
