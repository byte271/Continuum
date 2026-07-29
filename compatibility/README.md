# Compatibility corpus

This corpus measures unchanged, self-contained pure-Python programs. Every
program is MIT-licensed with the repository and is run as the same source file
under:

1. ordinary CPython;
2. uninterrupted Continuum;
3. Continuum freeze and restore in the harness process;
4. Continuum freeze and restore in a new Python process.

The runner compares stdout byte for byte. It retains compile and runtime
failures rather than excluding unsupported programs. Cross-platform status is
`not_run` until the exact corpus image is exercised by the native proof
workflow.

```bash
python3 -m compatibility.runner \
  --output compatibility/results/latest.json
```

The primary metric is the percentage of all corpus programs that compile, run,
freeze, resume in a new process, and match ordinary CPython output.
