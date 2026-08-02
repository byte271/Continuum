# Continuum Portable Process Image 0.2

## Container

The image is a ZIP container. Paths are POSIX relative paths. Readers reject
absolute paths, `..`, backslashes, directories, duplicate entries, unexpected
entries, oversized members, excessive total expansion, and suspicious
compression ratios. Readers never extract entries to caller-selected paths.

Required entries:

```text
manifest.json
runtime.json
code/program.py
code/ir.json
modules/hashes.json
heap/objects.json
frames/frames.json
resources/resources.json
checksums.json
```

Bundled files use:

```text
resources/files/<resource-id>.bin
```

## Manifest

`manifest.json` records:

- format and writer versions;
- source OS, architecture, and exact Python version;
- target compatibility rules;
- entry program and SHA-256;
- frame, heap-object, and open-file counts;
- top resume location;
- supported and unsupported resources;
- compression and integrity algorithms;
- the executable-content security boundary.
- mandatory runtime capabilities and whether native payload is required.

`frames/frames.json` is inspectable metadata. The authoritative restorable
state is the graph rooted in `heap/objects.json`.

### Optional `checkpoint` block

Images written by the rolling checkpoint writer carry one additional manifest
key. It is **optional**: manual `freeze` images and every image written before
this feature omit it entirely, and a reader that finds it absent must treat the
image as an ordinary image rather than as malformed.

```json
"checkpoint": {
  "checkpoint_format_version": "1",
  "mode": "periodic",
  "lineage_id": "cont-4e39c4f752d1",
  "generation": 7,
  "previous_generation": 6,
  "created_at": "2026-08-01T18:41:07.221845+00:00",
  "requested_interval_seconds": 0.1,
  "durability": {"file_fsync": true, "directory_fsync": "supported"}
}
```

Three deliberate decisions:

- **No new archive entry.** The block lives in `manifest.json`, which
  `checksums.json` already covers, so `generation` and `lineage_id` are
  authenticated by the container's existing integrity check. Recovery selects
  between slots using these fields, so they must not be forgeable without
  breaking the checksum. A separate metadata file outside the container would
  have been trusted input, which is exactly what this avoids.
- **No format-version bump and no new capability.** The block is provenance
  only and never affects the restore decision, so a runtime that predates it
  restores a checkpoint image correctly by ignoring it. Bumping
  `format_version` would have wrongly refused these images on older runtimes
  that can in fact read them.
- **Versioned, not free-form.** `checkpoint_format_version` is checked
  exactly; an unknown value is refused rather than partially interpreted, so a
  future revision cannot be silently misread by this runtime.

When present the block is structurally validated on both write and read. Every
field is checked; the list below is exactly what the implementation enforces:

| Field | Rule |
| --- | --- |
| `checkpoint_format_version` | must equal `"1"`; any other value is refused rather than partially interpreted |
| `mode` | must be a known mode (currently only `"periodic"`) |
| `lineage_id` | 1 to 128 characters from `A-Z`, `a-z`, `0-9`, `-`, `_`. **ASCII only** -- Unicode letters, Arabic-Indic digits, bidirectional controls, whitespace, `/`, and `.` are all refused, so two lineages cannot look identical in operator output while differing |
| `generation` | positive integer; `True` does not qualify as `1` |
| `previous_generation` | `null`, or a positive integer strictly less than `generation` |
| `created_at` | string, 1 to 64 characters |
| `requested_interval_seconds` | number in `(0, 86400]`; booleans refused |
| `durability.file_fsync` | must be exactly `true` |
| `durability.directory_fsync` | `"supported"` or `"unsupported-on-platform"` |

A malformed block makes the whole image invalid. The slot count is deliberately
**not** recorded here: it is fixed at two, so there is nothing to disagree about
between the writer and a recovering reader.

## IR

`code/ir.json` contains named function blocks and instructions. Control-flow
targets are integer instruction indices. `function_id` values are portable
identifiers, never addresses. The loader validates every opcode and jump
target before constructing a VM.

Function definitions include a validated `default_count`, added in IR 0.3 and
carried unchanged by the shipping IR 0.4.
`MAKE_FUNCTION` captures that many definition-time values from the operand
stack. Function heap records contain the resulting defaults tuple, preserving
mutable-default identity across calls and restoration.

`code/program.py` is evidence and permits future recompilation checks. Resume
does not execute it. Its SHA-256 must match the manifest,
`modules/hashes.json`, and the source identity embedded in IR.

## Heap graph

Every value is either an inline atom or a reference:

```json
{"t":"int","v":"123"}
{"t":"str","v":"hello"}
{"t":"ref","id":17}
```

Object records have canonical, zero-based IDs. Supported records include:

- `list`, `dict`, `tuple`, `set`, `frozenset`, `bytearray`, `range`;
- `random`;
- Continuum `function`, builtin, module, and bound-call references;
- portable iterators;
- allowlisted built-in exceptions;
- `resource_ref`.

Integers use decimal strings and floats use hexadecimal strings to avoid loss
through JSON number implementations. Bytes use base64. Mutable containers are
allocated before child references are filled, preserving shared identity and
cycles. Cycles that require an immutable object to exist before construction
are rejected in 0.1.

The format does not contain pickle data, machine pointers, executable native
pages, or assumed virtual addresses.

The decoder limits the object table to 2,000,000 records and graph nesting to
500 reference/value levels. Set and frozenset members are emitted in a stable
portable order. Duplicate/noncanonical IDs, invalid references, malformed
atoms, invalid ranges, and unknown kinds fail closed.

## Runtime state root

The graph root contains:

- module globals;
- ordered active frames;
- frame PCs, locals, operand stacks, control blocks, and finally reasons;
- program arguments and source path;
- instruction and safe-point counters;
- Python module-level random state.

## Resources

`resources/resources.json` records the capture policy and resource table.
Regular-file records contain:

- resource ID and original absolute path;
- open mode, encoding, error and newline policies;
- stream offset and closed state;
- size, nanosecond mtime, and SHA-256;
- whether content is bundled.

Write-capable files are rejected.

## Integrity and authenticity

`checksums.json` contains SHA-256 for every entry except itself and an optional
future `SIGNATURE` entry. Checksum coverage must exactly match the archive.

Checksums detect corruption and inconsistent mutation. They do not authenticate
the author. Version 0.1 does not implement signatures and reports
`signature: not-present`.

## Compatibility

The current writer requires:

- image format 0.2;
- IR 0.4;
- Continuum runtime 0.5.0a1;
- CPython 3.12.13 or 3.13.14, the exact versions in
  `abi.VERIFIED_PYTHON_VERSIONS`;
- target OS Linux, Darwin, or Windows;
- target architecture x86_64 or arm64;
- a target `(OS, architecture)` pair listed in `execution_contract.target`
  **and** accepted by the reading runtime;
- `native_payload_required: false`;
- only the mandatory capabilities implemented by this runtime.

`execution_contract.target.platforms` currently lists Linux x86_64, Linux
arm64, Darwin x86_64, Darwin arm64, and Windows x86_64. Windows arm64 is absent
and is rejected on the pair check.

The platform pair and the Python version are each decided twice: once against
the list the image carries, and once against the reading runtime's own accepted
list (`abi.VERIFIED_PLATFORMS`, `abi.VERIFIED_PYTHON_VERSIONS`). An image that
adds Windows arm64 to its own lists and recomputes every archive checksum is
still refused, because the runtime never accepted that pair.

Container format 0.1 images carry `target_compatibility` instead of
`execution_contract` and are read under the legacy exact-version rule.

These declarations describe what a reader will attempt to restore. They are
not evidence that the pair has been exercised: see
[PORTABILITY.md](PORTABILITY.md) for the combinations that have actually run.
Matching them is also not proof that every host library has identical
semantics. Only allowlisted standard-library modules are callable.

Archive member names stay POSIX-relative on every host, including Windows.
The absolute paths stored inside `resources/resources.json` are host-form
data, not archive paths, and are never used to open an entry.
