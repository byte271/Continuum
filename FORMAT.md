# Continuum Portable Process Image 0.1

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

## IR

`code/ir.json` contains named function blocks and instructions. Control-flow
targets are integer instruction indices. `function_id` values are portable
identifiers, never addresses. The loader validates every opcode and jump
target before constructing a VM.

IR 0.3 function definitions include a validated `default_count`.
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

- image format 0.1;
- IR 0.4;
- Continuum runtime 0.3.0;
- CPython 3.12.13;
- target OS Linux, Darwin, or Windows;
- target architecture x86_64 or arm64;
- a target `(OS, architecture)` pair listed in
  `target_compatibility.platforms`;
- `native_payload_required: false`;
- only the mandatory capabilities implemented by this runtime.

`target_compatibility.platforms` currently lists Linux x86_64, Linux arm64,
Darwin x86_64, Darwin arm64, and Windows x86_64. Windows arm64 is absent and
is rejected on the pair check.

These declarations describe what a reader will attempt to restore. They are
not evidence that the pair has been exercised: see
[PORTABILITY.md](PORTABILITY.md) for the combinations that have actually run.
Matching them is also not proof that every host library has identical
semantics. Only allowlisted standard-library modules are callable.

Archive member names stay POSIX-relative on every host, including Windows.
The absolute paths stored inside `resources/resources.json` are host-form
data, not archive paths, and are never used to open an entry.
