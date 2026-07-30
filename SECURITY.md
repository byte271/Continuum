# Security

A `.cont` image is executable, untrusted content. Passing integrity checks does
not make it safe to run.

## Implemented controls

- no general pickle or dynamic class import;
- closed allowlists for heap tags, VM opcodes, builtins, modules, and methods;
- exact format, IR, runtime, and Python compatibility checks;
- SHA-256 coverage for every payload entry;
- canonical object IDs and validated references;
- graph object-count and nesting limits, deterministic set encoding, and
  strict atom/range validation;
- bounded entry count, member size, total expanded size, and compression ratio;
- rejection of absolute paths, traversal segments, backslashes, duplicates,
  directory entries, and unexpected files;
- no archive extraction;
- cross-checked source, IR, runtime, capability, frame, heap, module, and
  resource metadata;
- deep `continuum verify` mode that reconstructs the allowlisted object graph
  and frame state without opening resources or dispatching program execution;
- read-only regular-file resources only;
- strict content verification for host-file rebinding;
- session control records created with mode `0600` inside directories created
  with mode `0700`, enforced by the OS on POSIX hosts only;
- atomic image and session-state replacement.

## Remaining risks

### Arbitrary code execution

The VM intentionally executes the bundled IR with the current user's
authority. The allowlist limits the current surface but is not a sandbox.
Untrusted images should run in an OS sandbox with no secrets and minimal file
access.

`continuum inspect` checks container structure, hashes, schemas, and
cross-document metadata. `continuum verify` goes further by allocating and
decoding allowlisted objects and reconstructing VM frames, but it does not run
the VM, compile bundled source, import application modules, or open file
resources. Both commands process attacker-controlled data; neither authenticates
the author nor proves that resuming the image is safe.

### Resource exhaustion

Archive, object counts, and decoder graph nesting are bounded, but allowed
computations can consume unbounded CPU and memory after resume. Run untrusted
images with process-level limits.

### Module substitution and dependency confusion

Only four standard-library names are currently importable. Exact Python
version is required. The image records that native or generated stdlib modules
cannot always have a meaningful portable source hash. A hostile Python
installation can still substitute those modules.

### Bundled files

Bundled bytes are read directly from their ZIP members and never extracted.
They may contain secrets. Anyone with the image can read them.

### Windows permission semantics

The runtime requests `0600` for session records and freeze control files and
`0700` for the directories under `CONTINUUM_HOME`. Those requests are POSIX
mode bits. On Windows, `os.chmod` only clears or sets the read-only attribute
and `os.open` mode bits are not an ACL, so the requested modes are not an
access-control guarantee. Access is instead governed by the inherited NTFS
ACLs of the containing directory, which for the default
`%USERPROFILE%\.continuum` normally restrict the user's profile but are not
verified by Continuum.

A session record and its freeze control files contain the session control
token and the image output path. Anyone who can read them can request a
checkpoint of that session, and anyone who can write the request path can
choose where the image is written. On Windows, treat `CONTINUUM_HOME` as
protected only to the degree its parent directory's ACLs already protect it,
and do not place it on a world-readable or removable volume. Continuum does
not audit or repair those ACLs.

This applies to session control only. Image integrity, the allowlists, and
every other control above are filesystem-independent.

### Secrets and environment

Reachable local variables and module globals are stored in plaintext inside
the ZIP. Environment variables are not automatically copied, but values the
program already read can be reachable and serialized. Continuum does not
redact secrets.

### Tampering and signatures

SHA-256 detects accidental or internally inconsistent changes only if the
attacker cannot also rewrite `checksums.json`. There is no signing or trust
store in 0.1. A future signature must cover the checksum document and a
canonical manifest.

### Symlinks and file races

Paths are resolved when opened. Strict/relocate verification hashes the target
before reopening it, leaving a possible time-of-check/time-of-use race. Future
versions should verify and open through one descriptor where host APIs permit.

### Durability gaps

The image file itself is fsynced before atomic replacement. Directory fsync is
best-effort and its failure is ignored for portability. This prevents a full
power-loss durability guarantee on every supported filesystem. Failure
injection does not yet cover each individual ZIP/checksum/fsync/rename stage.

## Reporting

Do not attach images that may contain secrets to a public issue. Report
security problems privately to the repository owner until a dedicated
security contact is established.
