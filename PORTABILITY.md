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

Regular-file metadata includes the source absolute path as data. `bundle`
does not use that path on the target. `relocate` maps it explicitly.

## Compatibility decision

Resume rejects an image unless all of these checks pass:

- format 0.1 and current IR 0.3 schema validation;
- Continuum runtime implementation and exact runtime version;
- CPython 3.12.13;
- target OS in `Linux`, `Darwin`;
- target architecture in `x86_64`, `arm64`;
- `native_payload_required` is false;
- every mandatory capability is recognized;
- source, IR, module, runtime, resource, frame, heap-count, and checksum
  documents agree.

Matching metadata is necessary, not sufficient evidence of portability.
Allowlisted standard-library calls execute the target host's CPython
implementation and can still have platform-specific behavior.

## Tested combinations

| Revision | Source | Target | Result |
| --- | --- | --- | --- |
| Current IR 0.3/runtime 0.2.0.dev0 | Current Linux x86_64 process | New process on same Linux x86_64 host | verified |
| Current IR 0.3/runtime 0.2.0.dev0 | Native Linux x86_64 | Native Apple Silicon macOS arm64 | unverified; new proof image required |
| Proof commit, IR 0.2/runtime 0.1.1.dev0 | Native GitHub-hosted Linux x86_64 VM | Native GitHub-hosted Apple Silicon macOS arm64 | **verified**; Actions run 30489463484, 26/26 conditions |
| Any revision | Linux x86_64 host | Second native Linux x86_64 environment | unverified |
| Any revision | Any other native cross-architecture pair | Any | unverified |

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
tested program, IR 0.2/runtime 0.1.1.dev0, resources, or platform pair. It is
not evidence for images written by current IR 0.3.

## Remaining host assumptions

- ZIP, JSON, SHA-256, file seeking, `fsync`, and atomic same-directory
  replacement behave as required by the host Python/OS.
- Session control relies on POSIX `SIGUSR1` for notification and
  same-filesystem hard links for atomic no-clobber request publication.
- Directory `fsync` is attempted but its failure is ignored for host
  compatibility; this weakens the power-loss durability claim on hosts that
  do not support it.
- Text-stream cookies are treated as offsets only for the exact supported
  Python version.
- Imported stdlib modules are allowlisted by name and exact Python version,
  not authenticated source hashes.
- Strict host-file verification has a hash/open time-of-check/time-of-use
  window.
