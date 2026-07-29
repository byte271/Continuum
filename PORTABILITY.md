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

IR 0.2 stores no Python `id()` values, machine pointers, native stack bytes,
executable pages, endianness-dependent structs, or host-sized packed
integers. Runtime instruction caches are reconstructed from IR and are not
serialized.

Regular-file metadata includes the source absolute path as data. `bundle`
does not use that path on the target. `relocate` maps it explicitly.

## Compatibility decision

Resume rejects an image unless all of these checks pass:

- format 0.1 and IR 0.2 schema validation;
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

| Source | Target | Result |
| --- | --- | --- |
| Current Linux x86_64 process | New process on same Linux x86_64 host | verified |
| Current Linux x86_64 process | Linux x86_64 dry-run of cross-platform package | verified; 6,603 combined output lines exactly matched uninterrupted control |
| Linux x86_64 host | Second native Linux x86_64 environment | unverified |
| GitHub-hosted Linux x86_64 VM | Native GitHub-hosted macOS arm64 | unverified |
| Any native cross-architecture pair | Any | unverified |

The Linux source half generated
`artifacts/cross-platform-linux-dry-run-2/linux-x86_64.cont`, recorded its
SHA-256, terminated the source PID, deleted the original input path, resumed
from the bundle in a new Linux process, and matched a control run. This is not
counted as cross-OS or cross-architecture proof.

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

No Apple Silicon host is available in the current environment, so
`target-evidence.json` has not been produced.

## Remaining host assumptions

- ZIP, JSON, SHA-256, file seeking, `fsync`, and atomic same-directory
  replacement behave as required by the host Python/OS.
- Session control currently relies on same-filesystem hard links for atomic
  no-clobber request publication.
- Directory `fsync` is attempted but its failure is ignored for host
  compatibility; this weakens the power-loss durability claim on hosts that
  do not support it.
- Text-stream cookies are treated as offsets only for the exact supported
  Python version.
- Imported stdlib modules are allowlisted by name and exact Python version,
  not authenticated source hashes.
- Strict host-file verification has a hash/open time-of-check/time-of-use
  window.
