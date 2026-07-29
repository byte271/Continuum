# Native GitHub Linux x86_64 to macOS arm64 validation

These scripts are the only accepted cross-platform validation path. They do
not change Continuum compatibility checks, recompile the application during
resume, or rewrite the `.cont` image.

The first complete proof passed in
[Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
at commit `15bceefece050d06a1f504244a77434e31fd5228`. The native
Linux x86_64 source job and native Apple Silicon macOS arm64 target job passed
all 26 conditions. The unchanged image SHA-256 was
`5a72261f61a3df2b71aec6882d3dbfc31196813a1bbfa5438cd9e9d069f324b9`.

That result is immutable proof for IR 0.2/runtime 0.1.1.dev0. The current
development revision writes IR 0.3 images. It must publish a clean commit,
generate a new Linux image, and pass this entire workflow before IR 0.3 gains
the same cross-platform status; the old image may not be reused.

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

## 2. Run on native GitHub Linux x86_64

Use the `linux-source` job on `ubuntu-24.04`. The job builds exact CPython
3.12.13 from the official source-only release. The pinned XZ tarball SHA-256
is:

```text
c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684
```

The source validator accepts evidence only when GitHub metadata identifies
the `linux-source` job as a GitHub-hosted Linux X64 runner. It:

- verifies Linux, exact `x86_64` in Python and `uname`, and `RUNNER_ARCH=X64`;
- rejects application-container and known QEMU/TCG/Bochs markers;
- records `/proc/cpuinfo`, `lscpu`, virtualization checks, `ImageOS`, and
  `ImageVersion`;
- verifies Python 3.12.13 and a clean Git tree;
- verifies that the current interpreter matches the independently recorded
  native CPython source build;
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
- hashes the image and changes its mode to read-only;
- writes `linux-evidence.sha256` over the complete source-phase evidence and
  makes those files read-only.

`--rehearsal` permits a local mechanics test. Rehearsal evidence is marked
disqualified and the macOS target script refuses it.

## 3. Transfer without mutation

The workflow creates a deterministic tar of the complete Linux evidence
directory and uploads the tar plus its SHA-256 as one Actions artifact. The
`macos-target` job has `needs: linux-source`, downloads that artifact, verifies
its SHA-256 before extraction, and retains the transferred archive identity.

The target script independently checks:

- the Git commit and clean tree;
- every tracked source-file hash;
- the repository archive hash;
- every file listed in `linux-evidence.sha256`;
- the image hash before resume.

Any mismatch stops validation.

## 4. Run on GitHub-hosted Apple Silicon macOS

The `macos-target` job runs on `macos-26`, which must report native Darwin
arm64. It independently builds CPython 3.12.13 from the same verified source
release. The workflow supplies the verified archive path, archive hash,
source-job result, source commit, extracted evidence, and target build
evidence to `target_macos.py`; invoking that script without the transferred
chain is intentionally rejected.

The script records raw `sw_vers`, `uname`, `arch`, Python, binary `file`,
CPU-brand, Rosetta, Git, and SHA-256 evidence. It rejects:

- a non-Darwin host or non-GitHub-hosted target;
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
`combined-output.log`, and evaluates all 26 required conditions in
`comparison.json`. It then writes `final-evidence.sha256` over the complete
successful evidence set and makes those files read-only.

The public entry point is:

```bash
gh workflow run .github/workflows/cross-platform-proof.yml --ref main
```

GitHub requires a `workflow_dispatch` workflow to exist on the repository's
default branch before it can be manually dispatched.

## Evidence files

A successful directory contains:

```text
git-commit.txt
source-tree.sha256
repository.tar
repository.sha256
linux-evidence.sha256
final-evidence.sha256
evidence-archive.sha256
image-transferred.sha256
github-linux-metadata.json
github-macos-metadata.json
linux-qualification.json
macos-qualification.json
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

PID numbers and wall-clock timestamps are recorded, but timestamps from two
independent hosts are not numerically compared because their clocks can be
skewed. Causal ordering is established by the target script verifying the
complete `linux-evidence.sha256` set—whose source evidence is written only
after the source process exits and is reaped—before it creates the new target
process.

A future run is successful only when it produces `target-evidence.json`, a
26-condition `comparison.json` whose values are all true, and a verified
`final-evidence.sha256`. Absence or failure of any of those files means that
specific run is not verified; it does not invalidate the immutable successful
baseline run above.
