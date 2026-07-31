# Portability

## Portable representation

The `.cont` container uses ZIP members whose payloads are UTF-8 JSON, UTF-8
source, or opaque bundled file bytes. Portable state uses:

- decimal strings for arbitrary-precision integers;
- hexadecimal strings for IEEE-style Python float values;
- base64 for bytes in the graph;
- canonical zero-based graph object IDs;
- logical IR instruction indices rather than code addresses;
- POSIX archive entry names;
- SHA-256 hashes written as lowercase hexadecimal text;
- explicit source OS, architecture, Python, runtime, IR, and capability
  metadata.

Current IR 0.3 stores no Python `id()` values, machine pointers, native stack bytes,
executable pages, endianness-dependent structs, or host-sized packed
integers. Runtime instruction caches are reconstructed from IR and are not
serialized.

Regular-file metadata includes the source absolute path as data. That path is
written in the source host's own form, including a Windows drive-letter path
when the source is Windows. `bundle` does not use that path on the target.
`relocate` maps it explicitly and accepts an absolute POSIX or Windows path as
its `OLD` key. `strict` requires the identical absolute path on the target and
therefore cannot succeed across hosts with different path forms.

## Compatibility decision

Resume rejects an image unless all of these checks pass:

- container format 0.2 (or legacy 0.1) and current IR 0.4 schema validation;
- Continuum runtime implementation. Container format 0.2 images no longer require an exact runtime version `0.5.0a1`; they require the execution ABI and the capability set. Container format 0.1 images still require it exactly;
- CPython 3.12.13 or 3.13.14 for container format 0.2 (the exact verified
  allowlist); exactly the creator's CPython for legacy format 0.1;
- target OS in `Linux`, `Darwin`, `Windows`;
- target architecture in `x86_64`, `arm64`;
- the exact target `(OS, architecture)` pair in the manifest platform list;
- `native_payload_required` is false;
- every mandatory capability is recognized;
- source, IR, module, runtime, resource, frame, heap-count, and checksum
  documents agree.

The accepted pairs are Linux x86_64, Linux arm64, Darwin x86_64, Darwin arm64,
and Windows x86_64. Windows arm64 is not an accepted pair and is rejected by
the pair check even though `Windows` and `arm64` each appear in the preceding
lists.

Accepting a target pair is a format-compatibility decision only. It states
that this runtime will attempt the restore, not that the pair has ever been
exercised. The tested combinations below are the only evidence of portability.
Allowlisted standard-library calls execute the target host's CPython
implementation and can still have platform-specific behavior.

## Tested combinations

| Revision | Source | Target | Result |
| --- | --- | --- | --- |
| Release IR 0.4/runtime 0.3.0 | Current Linux x86_64 process | New process on same Linux x86_64 host | verified; `runtime-bundles.yml` `linux-x86_64` |
| Release IR 0.4/runtime 0.3.0 | Current Apple Silicon macOS arm64 process | New process on same macOS arm64 host | verified; `runtime-bundles.yml` `macos-arm64` |
| Release IR 0.4/runtime 0.3.0 | Current Windows x86_64 process | New process on same Windows x86_64 host | verified; `runtime-bundles.yml` `windows-x86_64` |
| IR 0.3/runtime 0.2.0a1 | Native Linux x86_64 | Native Apple Silicon macOS arm64 | **verified**; Actions run 30509186641 at commit `3a4a43fb74331113225d7b9a3a0fef4afd1371fa` |
| Release IR 0.3/runtime 0.2.0 | Native Linux x86_64 | Native Apple Silicon macOS arm64 | **verified**; Actions run 30585208329 at commit `a73073d` |
| Proof commit, IR 0.2/runtime 0.1.1.dev0 | Native GitHub-hosted Linux x86_64 VM | Native GitHub-hosted Apple Silicon macOS arm64 | **verified**; Actions run 30489463484, 26/26 conditions |
| IR 0.4/runtime 0.2.0 | Native Linux x86_64 | Native Apple Silicon macOS arm64 | **verified**; Actions run 30592158078 at commit `21f7b2e`, carrying a class, an instance, a live handler, variadic bindings, and a shared closure cell |
| Release IR 0.4/runtime 0.3.0 | Native Linux x86_64 | Native Apple Silicon macOS arm64 | **verified**; Actions run 30596179154 at commit `023f74c` |
| Container format 0.2/IR 0.4/execution ABI 1.0/runtime 0.4.0a1 | Native Linux x86_64, **CPython 3.12.13** | Native Apple Silicon macOS arm64, **CPython 3.13.14** | **verified**; Actions run 30658976309 at commit `40cc9dd`, image SHA-256 `3b564d9d37a9353ebb22027a4b3597d30fc2eef1272c3a220fc7f65e3d939824` identical at capture, on arrival, and after restore; driven entirely through the public CLI; 4 live frames; 0 completed actions repeated |
| Any revision | Native Windows x86_64 | Any other platform | unverified; no workflow generates or resumes a cross-host Windows image |
| Container format 0.2 | Any CPython outside `abi.VERIFIED_PYTHON_VERSIONS` | Any | unverified and refused before execution; the allowlist is exact, so 3.13.0 and 3.12.14 are refused as firmly as 3.9 |
| Any revision | Any other platform | Native Windows x86_64 | unverified; no workflow generates or resumes a cross-host Windows image |
| Any revision | Linux x86_64 host | Second native Linux x86_64 environment | unverified |
| Any revision | Any other native cross-architecture pair | Any | unverified |

Same-host rows mean the complete test suite, including checkpoint,
source-process exit, and resume in a newly created process, ran natively on
that host. They are not evidence for any cross-host pair.

The cross-platform rows are produced by
`.github/workflows/cross-platform-proof.yml`, which has exactly two jobs,
`linux-source` and `macos-target`. Adding Windows to that proof is tracked
separately in [ROADMAP.md](ROADMAP.md) and has not been done.

The verified run is
[GitHub Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
at commit `15bceefece050d06a1f504244a77434e31fd5228`. Its Linux
source job ran on `ubuntu-24.04`; its dependent target job ran on `macos-26`
and recorded Darwin `arm64`, Apple M1, and
`sysctl.proc_translated=0`. Both jobs independently built exact CPython
3.12.13 from the same official source hash.

The continuation image SHA-256 was
`5a72261f61a3df2b71aec6882d3dbfc31196813a1bbfa5438cd9e9d069f324b9`
at source, after artifact transfer, before target resume, and after target
resume. The source stopped after iteration 64 and the target continued at
iteration 65 in a new process. Four frames, one operand item, one control
block, shared references, a cycle, RNG state, and a bundled read-only file at
offset 154 were restored. The 6,605 combined output lines matched the
uninterrupted control byte for byte and no irreversible action was duplicated.

## Validation package

See `validation/cross_platform/README.md`. The source and target scripts:

- refuse the wrong native OS/architecture;
- record `uname -a`, `uname -m`, Python and Continuum versions;
- record source/target PIDs and source exit;
- compare SHA-256 on both machines;
- preserve durable action logs;
- compare source-plus-target output byte-for-byte with an uninterrupted
  control;
- reject duplicate irreversible action records;
- compare final deterministic hashes.

The successful run produced both `target-evidence.json` and a verified
`final-evidence.sha256` manifest. The Linux and final Actions artifacts remain
attached to the exact workflow run. This result does not generalize beyond the
tested program, IR 0.2/runtime 0.1.1.dev0, resources, or platform pair.

IR 0.3 has its own passing run of the same two jobs at runtime 0.2.0a1, and
the 0.2.0 release repeats it rather than inheriting it, because the runtime
version is part of the image compatibility contract. Both are recorded in the
table above. Both results cover the same single direction. The
scripts in this package are `source_linux.py` and `target_macos.py`; there is
no Windows source or target script, so no Windows evidence of any kind exists
in this package.

## Remaining host assumptions

- ZIP, JSON, SHA-256, file seeking, `fsync`, and atomic same-directory
  replacement behave as required by the host Python/OS. Windows rejects
  `fsync` on a read-only CRT descriptor, so the image writer opens its own
  writable descriptor to obtain identical durability semantics on every host.
- Session control publishes a freeze request through a same-directory hard
  link, so `CONTINUUM_HOME` must be on a filesystem that supports hard links.
  This holds for typical POSIX filesystems and for NTFS; it does not hold for
  FAT32/exFAT volumes or for some network shares, where a freeze request
  cannot be published at all.
- Notification of a published request is host-specific. POSIX hosts deliver
  `SIGUSR1` and idle safe points read an in-memory Boolean without touching
  the filesystem. Windows has no `SIGUSR1`, so safe points poll for the
  request file at most once every 10 ms
  (`session.WINDOWS_REQUEST_POLL_INTERVAL_SECONDS`). The published image is
  identical; the idle cost and freeze latency are not.
- Session control files are created with mode `0600` inside directories
  created with mode `0700`. Those modes are enforced by the OS on POSIX
  hosts. On Windows, `os.chmod` only affects the read-only attribute, so the
  requested mode bits are not an access-control guarantee; NTFS inherited ACLs
  govern instead.
- Directory `fsync` is attempted but its failure is ignored for host
  compatibility; this weakens the power-loss durability claim on hosts that
  do not support it.
- Text-stream cookies are treated as offsets only for the exact supported
  Python version.
- Imported stdlib modules are allowlisted by name and exact Python version,
  not authenticated source hashes.
- Strict host-file verification has a hash/open time-of-check/time-of-use
  window.

## Live source-code migration (experimental, container format 0.2)

| Revisions | Source | Target | Status |
| --- | --- | --- | --- |
| A -> B, plan format 1.0 | Native Linux x86_64, CPython 3.12.13 | Native Apple Silicon macOS arm64, CPython 3.13.14 | **verified**; Actions run 30668706966 at commit `03da288`, image SHA-256 `fb847f3b1e7fab9921f46d97b4f6b996716e89bad7880ce5231f45d9ac8c4d80` identical at capture, on arrival, and after migration; 4 active frames and 20 bindings mapped totally; 30 action nonces each exactly once; 0 repeated; old revision's future behavior not executed, new revision's executed; zero oracle failures |

Everything outside the accepted edit classes in COMPATIBILITY.md is refused.
