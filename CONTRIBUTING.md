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
python3 -m unittest discover -v
python3 -m continuum run examples/anti_restart.py 300000
```

The project currently requires CPython 3.12.13. Keep changes dependency-free
unless a dependency closes a measured correctness or security gap.

Architecture changes should add an ADR under `docs/adr/`. Format changes need a
new format version and compatibility tests; do not silently reinterpret 0.1
images.
