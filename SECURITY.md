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
- read-only regular-file resources only;
- strict content verification for host-file rebinding;
- 0600 session control records inside 0700 directories;
- atomic image and session-state replacement.

## Remaining risks

### Arbitrary code execution

The VM intentionally executes the bundled IR with the current user's
authority. The allowlist limits the current surface but is not a sandbox.
Untrusted images should run in an OS sandbox with no secrets and minimal file
access.

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
