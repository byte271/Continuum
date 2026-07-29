# Real Linux x86_64 to macOS arm64 validation

These scripts are the only accepted cross-platform validation path. They do
not change Continuum compatibility checks, recompile the application during
resume, or rewrite the `.cont` image.

## 1. Prepare the source repository

Use a real Git repository containing the exact Continuum source:

```bash
git status --porcelain=v1
git rev-parse HEAD
```

The status output must be empty. The source script creates a deterministic
SHA-256 manifest for every tracked file and a `git archive` tar file for the
current commit. Git documents that `git archive HEAD` archives the named tree;
the generated archive is used as the transferred repository identity.

## 2. Run on real Linux x86_64

Use CPython 3.12.13. The evidence directory must be new, empty, and outside
the Git working tree:

```bash
python3 validation/cross_platform/source_linux.py \
  --output /absolute/path/continuum-attempt-001
```

The command refuses obvious container environments. It:

- verifies Linux and exact `x86_64`;
- verifies Python 3.12.13 and a clean Git tree;
- records raw environment command output;
- runs the complete test suite;
- creates `git-commit.txt`, `source-tree.sha256`, `repository.tar`, and
  `repository.sha256`;
- launches the unchanged public CLI workload;
- fsyncs irreversible source actions before requesting freeze;
- waits for and reaps the source process;
- deletes the original bundled input;
- verifies four frames, operand/control state, shared identity, a cycle, RNG,
  nonzero file offset, and bundled resource metadata;
- hashes the image and changes its mode to read-only.

`--rehearsal` permits a container-only mechanics test. Rehearsal evidence is
marked disqualified and the macOS target script refuses it.

## 3. Transfer without mutation

Copy the exact Git revision and the entire evidence directory to a physical
Apple Silicon Mac. Do not regenerate the image. Keep `repository.tar`,
`source-tree.sha256`, and all Linux logs beside it.

The target script independently checks:

- the Git commit and clean tree;
- every tracked source-file hash;
- the repository archive hash;
- the image hash before resume.

Any mismatch stops validation.

## 4. Run on native Apple Silicon macOS

Use CPython 3.12.13 running natively as arm64:

```bash
python3 validation/cross_platform/target_macos.py \
  --input /absolute/path/continuum-attempt-001
```

The script records raw `sw_vers`, `uname`, `arch`, Python, binary `file`,
CPU-brand, Rosetta, Git, and SHA-256 evidence. It rejects:

- a non-Darwin host;
- `uname -m` or `arch` other than `arm64`;
- Python reporting anything other than `Darwin` and `arm64`;
- a translated Rosetta process;
- a Python binary without arm64 support;
- a non-Apple CPU brand;
- Python other than 3.12.13;
- a dirty or different Git revision;
- rehearsal or otherwise unqualified source evidence.

It runs the complete macOS test suite before creating the target process. It
then runs only:

```bash
python3 -m continuum resume linux-x86_64.cont --file-policy bundle
```

After resume it hashes the image again, runs the uninterrupted control, writes
`combined-output.log`, and evaluates all 18 required conditions in
`comparison.json`.

## Evidence files

A successful directory contains:

```text
git-commit.txt
source-tree.sha256
repository.tar
repository.sha256
linux-environment.txt
macos-environment.txt
source-evidence.json
target-evidence.json
comparison.json
linux-x86_64.cont
image-source.sha256
image-target-before.sha256
image-target-after.sha256
source-stdout.log
source-stderr.log
target-stdout.log
target-stderr.log
control-stdout.log
control-stderr.log
combined-output.log
full-test-linux.txt
full-test-macos.txt
```

Failures write `failure.json` and retain everything produced before the
failure. Never reuse a failed attempt directory. Fixes require a new commit and
a new Linux image in a new attempt directory.

PID numbers are recorded but are not required to differ because numeric PIDs
can be reused. The required proof is that the source exit and reap timestamps
precede creation of a separate target process.

No `target-evidence.json` or successful `comparison.json` means cross-platform
restoration remains unverified.
