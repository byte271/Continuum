# Contributing

Continuum accepts narrow, evidence-backed improvements.

## Ground rules

- Never add a fallback that restarts the source or replays completed work.
- A new supported construct needs a continuation test that freezes while its
  state is active.
- An unsupported construct must fail explicitly.
- Do not weaken image validation to make a test fixture load.
- Do not claim a platform until a native environment of that OS and
  architecture ran the step being claimed. Same-host and cross-host are
  separate claims: running the suite natively on a host is evidence for
  same-host continuation there and for nothing else. A cross-platform claim
  requires that pair's source and target jobs, in that direction.
- Accepting a target `(OS, architecture)` pair in the image format is a
  compatibility decision, never evidence. Do not cite the accepted-pair list
  as proof that a migration path works.
- Store exact commands and raw outputs for published performance numbers, and
  name the host they were measured on.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compatibility.runner \
  --output /tmp/continuum-compatibility.json
python3 -m continuum doctor
python3 -m continuum demo --output-dir /tmp/continuum-demo
```

On Windows:

```powershell
python -m unittest discover -s tests -v
python -m compatibility.runner --output $env:TEMP\continuum-compatibility.json
python -m continuum doctor
python -m continuum demo --output-dir $env:TEMP\continuum-demo
```

The project currently requires CPython 3.12.13 and supports Linux x86_64,
Apple Silicon macOS arm64, and Windows x86_64. A change to session control,
packaging, or path handling should state which of the three it was exercised
on. Keep changes dependency-free unless a dependency closes a measured
correctness or security gap.

Architecture changes should add an ADR under `docs/adr/`. Format changes need a
new format version and compatibility tests; do not silently reinterpret 0.1
images.

Before submitting a language feature, include CPython differential coverage,
freeze at every reachable safe point while the feature state is live, and a
malformed-image test if the portable representation changes. Cross-platform
status requires a newly generated image from the same clean commit on both
jobs; an earlier revision's proof cannot be reused for a later revision.

The cross-platform proof workflow currently has two jobs, `linux-source` and
`macos-target`. Windows is a natively supported host with same-host evidence
only, so no change may introduce a documentation claim, doctor field, or test
name asserting a Windows cross-platform path until that leg exists.
