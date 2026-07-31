# Self-contained runtime bundles

Continuum requires exact CPython 3.12.13. The supported installation design is
a platform archive containing its own interpreter:

| Platform | Bundle | Builder | Installer | Command |
| --- | --- | --- | --- | --- |
| Linux x86_64 | `continuum-linux-x86_64.tar.gz` | `build_bundle.sh` | `install.sh` | `bin/continuum` |
| macOS arm64 | `continuum-macos-arm64.tar.gz` | `build_bundle.sh` | `install.sh` | `bin/continuum` |
| Windows x86_64 | `continuum-windows-x86_64.zip` | `build_bundle_windows.ps1` | `install.ps1` | `bin\continuum.cmd` |

Each bundle contains the Continuum CLI, the exact CPython runtime, a runtime
identity manifest including the Continuum and IR versions, CPython build
evidence, the demonstration source, the license, and installation
documentation. The launcher sets `PYTHONHOME` and `PYTHONPATH` to the bundle
before starting its private interpreter.

Every build verifies the bundle before archiving it: `--version`,
`doctor --json` retained as `doctor-build-check.json`, and a real program run
through the launcher.

## POSIX hosts

Build a bundle in an empty output directory:

```bash
packaging/build_bundle.sh linux-x86_64 /absolute/output
packaging/build_bundle.sh macos-arm64 /absolute/output
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

## Windows x86_64

The Windows bundle is a `.zip`, not a `.tar.gz`, and the scripts are
PowerShell. Build it in a new output parent directory:

```powershell
.\packaging\build_bundle_windows.ps1 -OutputParent C:\absolute\output
```

`build_bundle_windows.ps1` calls `validation\windows\build_cpython.ps1`, which
downloads the same pinned official CPython 3.12.13 source archive, verifies the
same SHA-256, and builds it natively with MSBuild against the installed
platform toolset. The build refuses to continue unless the host reports Windows
x86_64 and the built interpreter reports exactly 3.12.13. It produces
`continuum-windows-x86_64.zip`, a `.sha256` sidecar, and a
`continuum-windows-x86_64-build-evidence` directory.

Install a verified local or HTTPS archive:

```powershell
.\packaging\install.ps1 `
  -Archive C:\path\to\continuum-windows-x86_64.zip `
  -Sha256 <64-lowercase-hex-digest>
```

`-Prefix` defaults to `%LOCALAPPDATA%\Continuum`. The installed command is
`<prefix>\bin\continuum.cmd`, a launcher that forwards to
`<prefix>\lib\continuum-windows-x86_64\bin\continuum.cmd`; POSIX prefixes use a
symlinked `bin/continuum` instead.

## Installer guarantees

Both installers require the expected digest, refuse HTTP, validate every
archive path, refuse to overwrite an existing installation, and run
`continuum doctor` before moving the bundle into place. `install.ps1`
additionally rejects duplicate archive members under Windows case-insensitive
comparison, reserved Windows device names, members whose Windows-normalized
form differs from the stored name, and any symbolic-link member.

## Verification status

`.github/workflows/runtime-bundles.yml` runs all three builds on every push to
`main`. Each job builds the bundle, verifies the moved archive against its
sidecar digest, extracts it, runs `continuum doctor`, runs the complete test
suite against the bundled interpreter, installs into a fresh prefix, and runs
`continuum doctor` again from the installed prefix. The Windows job also runs a
complete `continuum demo` continuation against the moved bundle.

That is evidence of a working native installation on each host. It is not
evidence that an image moves between hosts; see `PORTABILITY.md`.

All three archives are published with immutable release URLs and SHA-256
sidecars as of v0.3.0, so the main README documents the download and install
commands. Each digest below is the one CI recorded and the release upload was
verified against:

```text
continuum-linux-x86_64.tar.gz   8cd80c2d0094be1331107f2b8762085271112c4655dc853d4050cfaa9d3ec9f1
continuum-macos-arm64.tar.gz    1237449dff8d5d92db39ad36e156455784e08156e040348559b0609e90a1f009
continuum-windows-x86_64.zip    05c41f9b50858c400cadd6ad52b051e02a91ba22f8fac6d70df7c9a4eee1b0e0
```

Never install an archive without passing its expected digest; the installers
require it.
