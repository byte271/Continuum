# Limitations

## Python language

The current runtime accepts a useful but small subset:

- function definitions with required positional parameters;
- assignments, arithmetic, comparisons, Boolean expressions;
- `if`, `while`, portable `for` iteration, and loop `else`;
- function calls and nested active Continuum frames;
- lists, dictionaries, sets, tuples, slicing, and subscripting;
- module globals;
- `try/finally` without control transfer out of the `try`;
- a closed builtin, method, and stdlib-module allowlist.

Not supported:

- classes and arbitrary instances;
- closures, `nonlocal`, and `global`;
- decorators and default/keyword-only/variadic parameters;
- generators, coroutines, async code, and context managers;
- comprehensions and chained comparisons;
- `try/except`, `with`, `yield`, pattern matching, and every Python syntax form
  not explicitly compiled;
- monkey patching and dynamic code generation;
- arbitrary imports.

Unsupported syntax fails during compilation. Unsupported live values fail the
freeze and leave the source computation running.

See `LANGUAGE_SUPPORT.md` for the test-backed feature-by-feature matrix.

## Execution model

- CPython 3.12.13 exactly;
- one Continuum VM and one application thread;
- freeze only at compiler-inserted safe points;
- host builtin/module calls are atomic and cannot be suspended internally;
- no ordinary CPython frame or arbitrary PID attachment;
- no active native-extension state;
- no subprocesses, sockets, locks, signals, terminals modes, or child
  relationships;
- no JIT and substantial interpreter slowdown.

The target terminal is simply the stdout/stderr of `continuum resume`; previous
terminal screen contents are not recreated.

## Objects

The graph codec preserves shared identity and cycles for supported mutable
containers. It does not preserve implementation-specific identity of inline
immutable atoms. Cycles that require an immutable or wrapper object to be
constructed before any mutable anchor are rejected; list/tuple cycles with a
mutable anchor can be reconstructed. Hash objects and other native values are
allowed only as temporary values between safe points; if reachable at
checkpoint they cause an explicit error.

## Files

Only read-only regular files are supported. `bundle` restores a byte or text
stream in memory, not a filesystem descriptor with every platform-specific
attribute. Text stream position requires the exact supported Python version.
Write modes, mmap, pipes, devices, sockets, and directory handles are rejected.

## Portability

Images intentionally contain JSON, source, bundled data, and ZIP metadata—no
native code or pointers. Native Linux x86_64 to Apple Silicon macOS arm64
continuation was verified for the controlled proof workload in
[Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484).
That single platform pair and workload do not establish portability for other
programs, resources, operating systems, architectures, or Python versions.
See [STATUS.md](STATUS.md) and [PORTABILITY.md](PORTABILITY.md).
