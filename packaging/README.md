# Self-contained runtime bundles

Continuum requires exact CPython 3.12.13. The supported installation design is
a platform archive containing its own interpreter:

```text
continuum-linux-x86_64/
continuum-macos-arm64/
```

Each bundle contains the Continuum CLI, the exact CPython runtime, a runtime
identity manifest, CPython build evidence, the demonstration source, the
license, and installation documentation. The launcher sets `PYTHONHOME` and
`PYTHONPATH` to the bundle before starting its private interpreter.

Build a bundle in an empty output directory:

```bash
packaging/build_bundle.sh linux-x86_64 /absolute/output
```

The build downloads the official CPython 3.12.13 source archive, verifies the
pinned SHA-256, builds it natively, moves the installation to its final bundle
layout, runs `continuum doctor`, runs a source program through the bundle, and
creates a deterministic `.tar.gz` plus SHA-256 sidecar.

Install a verified local or HTTPS archive:

```bash
packaging/install.sh \
  --archive /path/to/continuum-linux-x86_64.tar.gz \
  --sha256 <64-lowercase-hex-digest>
```

The installer requires the expected digest, refuses HTTP, validates every
archive path, refuses to overwrite an existing installation, and runs
`continuum doctor` before moving the bundle into place.

Published one-line download commands must not be added to the main README
until both platform archives have passed clean-runner CI and immutable release
URLs and hashes exist.
