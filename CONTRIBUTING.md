# Contributing

Continuum accepts narrow, evidence-backed improvements.

## Ground rules

- Never add a fallback that restarts the source or replays completed work.
- A new supported construct needs a continuation test that freezes while its
  state is active.
- An unsupported construct must fail explicitly.
- Do not weaken image validation to make a test fixture load.
- Do not claim a platform until a native environment of that OS and architecture ran the
  source or target step.
- Store exact commands and raw outputs for published performance numbers.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compatibility.runner \
  --output /tmp/continuum-compatibility.json
python3 -m continuum doctor
python3 -m continuum demo --output /tmp/continuum-demo
```

The project currently requires CPython 3.12.13. Keep changes dependency-free
unless a dependency closes a measured correctness or security gap.

Architecture changes should add an ADR under `docs/adr/`. Format changes need a
new format version and compatibility tests; do not silently reinterpret 0.1
images.

Before submitting a language feature, include CPython differential coverage,
freeze at every reachable safe point while the feature state is live, and a
malformed-image test if the portable representation changes. Cross-platform
status requires a newly generated image from the same clean commit on both
jobs; the immutable IR 0.2 proof cannot be reused for IR 0.3.
