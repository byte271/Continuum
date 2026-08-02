# Limitations

## Python language

The current runtime accepts a useful but small subset:

- function definitions with required and defaulted positional parameters;
- complete argument binding: positional-only and keyword-only boundaries,
  `*args`, `**kwargs`, keyword-only defaults, and `*`/`**` call unpacking;
- assignments, arithmetic, comparisons, Boolean expressions;
- `if`, `while`, portable `for` iteration, and loop `else`;
- function calls and nested active Continuum frames;
- identity-preserving lexical closures, `nonlocal`, and multi-level capture;
- VM-owned classes and instances: methods, `__init__`, class and instance
  attributes, and attribute assignment, without inheritance;
- lists, dictionaries, sets, tuples, slicing, and subscripting;
- module globals;
- `try/finally` without control transfer out of the `try`, and portable
  `try/except` including tuple matching, `as` binding, and `else`;
- a closed builtin, method, and stdlib-module allowlist.

Not supported:

- inheritance, base classes, metaclasses, class decorators, descriptors, and
  user-defined exception classes;
- `global`;
- decorators;
- generators, coroutines, async code, and context managers;
- comprehensions, generator expressions, lambdas, and chained comparisons;
- chained assignment, `with`, `yield`, pattern matching, and every Python
  syntax form not explicitly compiled;
- monkey patching and dynamic code generation;
- arbitrary imports.

Unsupported syntax fails during compilation. Unsupported live values fail the
freeze and leave the source computation running.

See `LANGUAGE_SUPPORT.md` for the test-backed feature-by-feature matrix.

## Execution model

- CPython 3.12.13 or 3.13.14, exactly. The allowlist is exact and never a
  range: an interpreter that is merely *between* verified versions, such as
  3.13.0, is refused before any execution state is created or reconstructed.
  Adding a version requires a green native cross-Python proof run;
- one Continuum VM and one application thread;
- freeze only at compiler-inserted safe points;
- host builtin/module calls are atomic and cannot be suspended internally;
- no ordinary CPython frame or arbitrary PID attachment;
- no active native-extension state;
- no subprocesses, sockets, locks, application signal state, terminal modes, or child
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
[Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
for IR 0.2, and again for IR 0.3/runtime 0.2.0a1 in
[Actions run 30509186641](https://github.com/byte271/Continuum/actions/runs/30509186641).
That single platform pair and workload do not establish portability for other
programs, resources, operating systems, architectures, or Python versions.

Continuum runs natively on Linux x86_64, Apple Silicon macOS arm64, and
Windows x86_64, and same-host continuation is CI-verified on each. Running
natively on a host says nothing about moving an image to or from it. No
cross-platform path involving Windows has been run in either direction, and
no cross-host test has exercised a text-mode resource between hosts whose
native line endings differ. Windows arm64 is unsupported and images reject it.

The published 50-program compatibility corpus has been measured on Linux
x86_64 and, for the current IR 0.4 revision, on Windows x86_64. It has not been
measured on macOS arm64. The test suite exercises two corpus programs through
all four gates on every host, which is a smoke check of the runner rather than
a compatibility measurement for that host.

See [STATUS.md](STATUS.md) and [PORTABILITY.md](PORTABILITY.md).

## Live source-code migration (experimental)

Only the accepted edit classes in COMPATIBILITY.md are migrated; everything
else is refused, and refusal is the default for anything the mapper cannot
prove safe.

Two limits are worth stating plainly because they are not obvious:

**Edits to already-completed effects are accepted, not refused.** If a
statement executed before the checkpoint, editing it in the new revision has no
effect on the resumed run, and Continuum does not detect that and does not
refuse it. The migration is still sound -- nothing is replayed and nothing is
corrupted -- but an author who edits a line that already ran will see the old
behavior in the combined output and no diagnostic saying why. Detecting this in
general would require knowing which statements have executed, which a resume
position alone does not tell you for code inside a loop. This is a mandatory
refusal case that is **not** implemented.

**A refusal is not proof that an edit is unsafe.** The accepted-edit set is
deliberately narrow, so edits that are in fact harmless are refused when the
mapper cannot prove them safe. In a full sweep of one revision pair across
every applicable safe point, one position out of 197 refused an edit that a
person would consider acceptable, because execution had reached the very
statement being changed.

Migration is verified for one workload, one platform pair, and the revision
pairs in `validation/live_migration/`. It is not a general hot-reload facility.

## Rolling crash-recovery checkpoints

These are six different things. Do not read a claim about one as a claim about
another:

1. **Manual terminating freeze** (`continuum freeze`) -- commits one image and
   the source process exits. Unchanged by this feature.
2. **Non-terminating periodic checkpoint** (`continuum run --checkpoint-dir`)
   -- commits an image at a safe point and the same process keeps running.
3. **Crash recovery** (`continuum recover`) -- resumes the newest valid
   checkpoint after the process died.
4. **Cross-machine migration** -- moving an image to another host.
5. **Cross-Python continuation** -- restoring under a different verified
   interpreter.
6. **Live source-code migration** -- resuming into edited source.

A checkpoint image is an ordinary image, so 4 and 5 are *format*-compatible
with it. That is not the same as having been proven: no checkpoint image has
been moved between hosts and resumed by a proof workflow, so **cross-platform
rolling recovery is not claimed**.

**How much progress a crash can cost.** *Provided due checkpoints keep
committing successfully*, roughly one checkpoint interval of execution, plus the
time to reach the next safe point, plus commit time. Work performed after the
last committed generation is re-executed on recovery. This is visible and
expected: in the end-to-end test the recovered process re-emits the markers
produced between the last commit and the kill.

**That bound is not unconditional.** The default `--checkpoint-failure continue`
keeps the program running when a commit fails -- a full disk, a permission
change, a value the graph codec cannot encode. Every failure is reported loudly
on stderr and surfaced by `continuum checkpoints` as `last_error`, but the
program does not stop, so the newest valid generation can be **much** older than
one interval. Nothing in Continuum bounds how far behind it drifts. Monitor the
failure count, or choose `--checkpoint-failure terminate`.

**There is no fallback before the first commit.** A new checkpoint directory
holds no valid generation until one checkpoint has committed. The two-slot
guarantee -- that a previously committed checkpoint survives while the next one
is written -- begins after the first successful commit, not before it.

**The newest checkpoint can be lost; an older one should remain.** If the
process dies mid-commit, the temporary file is discarded and the previously
committed generation is selected. Two slots exist precisely so the newest
committed checkpoint is never the file being overwritten.

**Power-loss durability is conditional, not absolute.** Contents are flushed
before the atomic replace, and the directory entry is flushed after it *where
the platform supports that*. On POSIX the directory flush is performed and a
genuine I/O error fails the commit rather than being ignored; an
`EINVAL`/`ENOTSUP` answer is recorded as the weaker
`directory_fsync: "unsupported-on-platform"` state instead of being treated as
success. **On Windows there is no directory-entry flush**, so a power cut can
in principle lose the rename that publishes the newest generation even though
its contents reached storage; the previous generation remains. Beyond that,
durability depends on the filesystem and on the drive honouring flushes, which
Continuum cannot verify. **No power-loss testing on real hardware has been
performed.** The crash tests kill processes; they do not cut power.

**External side effects are not exactly-once.** Recovery re-executes the window
between the last checkpoint and the crash. Anything that window did outside the
controlled runtime -- network requests, database writes, messages, payments,
writes to unsupported external files -- can happen a second time. Continuum's
anti-replay guarantees apply to the controlled proof workload and its external
auditor, not to arbitrary external systems. True exactly-once effects require
idempotency keys, transactional integration, or an external auditor.

**Writable files remain unsupported.** Checkpointing does not broaden the
resource model: read-only regular files under strict, relocate, and bundle
policies are what is supported, exactly as before.

**Every checkpoint writes a full image.** Bundled read-only resources are
re-copied into every generation. At 100 ms this is real write amplification;
`PERFORMANCE.md` has measured figures. No incremental or shared-blob format is
implemented, deliberately: an external blob reference shared between slots
would break the property that each slot is independently valid.

**100 ms is a request, not a promise.** Whether it is achievable depends on
state size, host, and filesystem. On the measured Linux host a small-state
workload paused ~9 ms per checkpoint and a larger object graph ~18 ms, so 100 ms
was comfortably met; a large enough heap will not meet it. When a commit
overruns the interval the scheduler coalesces the missed deadlines into one
rather than queueing them.
