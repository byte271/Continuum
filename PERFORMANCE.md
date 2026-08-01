# Performance

## Current measurements (0.5.0a1, IR 0.4, execution ABI 1.0, plan format 1.0)

The migration figures below measure `plan-upgrade`, `verify-upgrade`, and
`resume --upgrade`, which are 0.5.0a1 features. The recorded
`continuum_version` inside the raw result files reads `0.4.0a1`, because the
benchmarks were taken before the version bump; the code measured is the code
in this release.

Measured 2026-07-31 on one Linux x86_64 host with
`benchmarks/measure_migration.py`, 20,000 workload iterations, 7 repetitions,
medians. The workload carries four active frames, a closure cell, a shared
mutable reference, a reference cycle, and VM-owned class and instance state.

Raw results:
`benchmarks/results/migration-linux-x86_64-python3.12.13-2026-07-31.json` and
`benchmarks/results/migration-linux-x86_64-python3.13.14-2026-07-31.json`.

| Figure | CPython 3.12.13 | CPython 3.13.14 |
| --- | ---: | ---: |
| Compile overhead (source to IR) | 1.0 ms | 1.0 ms |
| Continuum run (800,087 instructions) | 0.565 s | 0.618 s |
| Same program under CPython | 3.9 ms | 3.0 ms |
| **Slowdown vs. CPython** | **144x** | **206x** |
| Safe-point callback overhead | +0.2% | within noise |
| Freeze latency | 7.0 ms | 6.7 ms |
| Image size | 12,296 B | 12,308 B |
| Migration-plan generation | 9.7 ms | 13.8 ms |
| Migration-plan verification | 11.2 ms | 16.7 ms |
| Migration-plan size | 4,547 B | 4,549 B |
| Resume latency (restore only) | 2.0 ms | 2.0 ms |
| Resume + apply migration | 2.0 ms | 2.2 ms |

### Reading these honestly

**The slowdown is large and is the headline cost.** Continuum interprets its
own IR in Python, so a two-order-of-magnitude penalty against CPython running
the same source is expected and is not a regression to be explained away. It is
the price of execution state that can be serialized and moved. Nothing here
makes Continuum appropriate for hot loops.

The 3.13.14 slowdown ratio is worse than 3.12.13 in both directions at once:
Continuum is slower there *and* CPython is faster there. Both effects are real
and both are one host, one workload.

**Safe points are effectively free.** Attaching a live session's callback costs
under a percent, and on 3.13.14 the difference sits inside run-to-run noise.
Checkpointing is cheap to leave enabled; that is a deliberate design outcome,
since a safe point that cost real time would push users to disable it.

**Migration costs are dominated by verification, on purpose.** Verifying a plan
costs slightly *more* than generating it, because verification does not trust
the plan: it independently re-derives the whole mapping from the image and the
plan's own new source and compares. Paying roughly double to refuse a tampered
plan is the intended trade.

**Applying a migration is nearly free at resume time.** All the analysis happens
in `plan-upgrade` and `verify-upgrade`; `apply_plan` only rewrites frame
positions and swaps the IR, which is why resume with and without a migration are
indistinguishable at this workload size.

### Scope of these numbers

One host, one workload, one architecture. Nothing on this page was measured on
macOS arm64 or Windows x86_64, and no figure should be quoted for those hosts.
No workflow runs these benchmarks, so performance can still regress silently
between releases; wiring them into CI is not done.

## Historical measurements (0.1.1.dev0, IR 0.2)

> The section below predates the current runtime by two IR revisions and is
> retained as history. Do not quote it for 0.5.0a1.

Correctness remains the priority. These are raw local measurements from one
Linux x86_64 environment, not portable targets.

Every number on this page is Linux x86_64. Nothing here was measured on macOS
arm64 or Windows x86_64, and no number should be quoted for those hosts.

## Audited measurement

Command:

```bash
PYTHONPATH=. python3 benchmarks/measure.py \
  --iterations 10000 --repetitions 5 \
  --output benchmarks/results/linux-x86_64-python3.12.13-2026-07-29-audited.json
```

Environment: Continuum 0.1.1.dev0, CPython 3.12.13, Linux
6.12.13 x86_64.

| Metric | Raw result |
| --- | ---: |
| AST/IR compile median | 0.000337 s |
| Native control median | 0.001044 s |
| VM median, no callback | 0.156429 s |
| VM slowdown median | 149.80× |
| VM median, request-path polling | 0.258208 s |
| Polling / no-callback ratio | 1.651× |
| Complete image commit | 0.020604 s |
| Resource snapshot | 0.000012 s |
| Graph encoding | 0.007808 s |
| Heap JSON encoding | 0.003235 s |
| Image load and validation | 0.005845 s |
| Resource/graph/frame restoration | 0.002783 s |
| Remaining half execution | 0.083509 s |
| Image size | 21,467 bytes |
| Expanded payload | 139,359 bytes |
| Compression ratio | 6.492× |
| Heap objects | 18 |

Every individual timing sample is in the JSON result. Memory tracing is a
separate untimed VM run.

## Safe-point notification measurement

The post-proof baseline was recorded before changing session notification:

```bash
PYTHONPATH=. python3 benchmarks/measure.py \
  --iterations 10000 --repetitions 5 \
  --output benchmarks/results/linux-x86_64-python3.12.13-2026-07-29-post-proof-baseline.json
```

The identical workload was then measured after replacing per-safe-point
request-path lookups with an in-memory Boolean set by `SIGUSR1`:

```bash
PYTHONPATH=. python3 benchmarks/measure.py \
  --iterations 10000 --repetitions 5 \
  --output benchmarks/results/linux-x86_64-python3.12.13-2026-07-29-signal.json
```

Both runs used CPython 3.12.13 on Linux 6.12.13 x86_64. They are separate
five-sample runs, so the comparison does not treat native-control jitter as a
runtime improvement.

| Metric | Path-polling baseline | Signal/Boolean |
| --- | ---: | ---: |
| VM median, no callback | 0.141965 s | 0.141613 s |
| VM median, session callback | 0.252846 s | 0.151302 s |
| Callback / no-callback ratio | 1.781× | 1.068× |
| VM / native-control ratio | 149.60× | 159.00× |

The session-callback median fell 40.2%, and its overhead over the corresponding
no-callback VM run fell from 78.1% to 6.8%. The underlying VM median did not
materially change. The apparently worse VM/native ratio in the second run
comes from its lower 0.000891-second native-control median, not a slower VM.
All raw samples are retained in the two JSON files.

That improvement is specific to hosts with `SIGUSR1`. Windows has no
`SIGUSR1`, so its sessions keep a bounded request-path lookup on the idle safe
point, capped at one lookup per 10 ms. Structurally that is the
path-polling column above with a rate limit, but it has not been measured:
there is no Windows benchmark run, and the polling-baseline numbers here must
not be reused as a Windows estimate. Measuring it is a milestone in
`ROADMAP.md`.

## Why the prior 590× number was invalid

The original script timed the native loop normally, then enabled
`tracemalloc` around the VM timing. That attributed tracing overhead only to
Continuum. The old JSON is retained as historical raw output, but the 589.96×
ratio must not be used as a runtime claim.

## Profile and one optimization

Pre-optimization command:

```bash
PYTHONPATH=. python3 -m benchmarks.profile_runtime \
  --iterations 20000 \
  --profile benchmarks/results/runtime-2026-07-29.prof \
  --report benchmarks/results/runtime-2026-07-29.txt
```

The 480,027 calls to `VirtualMachine.step` were the largest cost: 0.511 s
self-time and 1.486 s cumulative in a 1.602 s profiled run. The only
optimization made was therefore to:

- predecode instruction dictionaries into runtime-only tuples;
- inline the run-loop form of `step`, while preserving public single-step
  execution.

The optimization described in this historical profile introduced IR 0.2.
Positional default-argument state arrived in IR 0.3; the current revision is
IR 0.4. Runtime
instruction caches remain unpersisted and are recreated after restore.

Post-optimization profile:

```bash
PYTHONPATH=. python3 -m benchmarks.profile_runtime \
  --iterations 20000 \
  --profile benchmarks/results/runtime-2026-07-29-optimized.prof \
  --report benchmarks/results/runtime-2026-07-29-optimized.txt
```

The same profiled workload fell from 1.602 s to 1.196 s (25.3%). The largest
remaining region is `_execute`: 0.454 s self-time and 0.822 s cumulative.
The later signal/Boolean change above removed request-path polling from idle
safe points without changing checkpoint image contents.

No native extension, JIT, or architecture-specific acceleration was added.

## Rolling checkpoints

Measured with `benchmarks/measure_checkpoints.py`. Raw JSON is in
`benchmarks/results/checkpoints-linux-x86_64.json`.

**These are Linux x86_64 figures from one host and do not transfer to macOS or
Windows.** No macOS or Windows checkpoint benchmark has been run. The host was
a 4-CPU Linux x86_64 container on an overlay filesystem, CPython 3.12.3,
60000 loop iterations per workload.

| Workload | State | Pause (median) | Pause (p95) | Commit (median) | Image | Recovery scan |
| --- | --- | --- | --- | --- | --- | --- |
| small-state | flat counters | 9.4 ms | 10.4 ms | 10.1 ms | 8.0 KiB | 2.7 ms |
| large-graph | 400 rows, shared refs + cycle | 17.5 ms | 22.3 ms | 18.1 ms | 17.1 KiB | 5.9 ms |

Scheduling overhead when checkpointing is enabled but no checkpoint is due --
the cost paid at every safe point -- was **0.07 µs/safe-point** (small-state)
and **0.35 µs/safe-point** (large-graph) over ~180000 safe points.

*Pause* is the stop-the-world serialization time: the program is not running
during it. It is reported separately from the requested interval on purpose,
because the interval is a scheduling target and the pause is the actual cost.

At a 100 ms requested interval both workloads kept up with **zero coalesced
ticks**: the pause is 9-22% of the interval. A large enough heap will not keep
up, at which point missed deadlines collapse into a single next checkpoint
rather than queueing.

**Write amplification is total, by design.** Every checkpoint writes a complete
image, including any bundled read-only resources. Over the measured runs that
was 24 KiB written for 3 commits (small-state) and 120 KiB for 7 commits
(large-graph) -- 3.0x and 7.0x the size of one image. Sustained 100 ms
checkpointing of a large bundled workload will write a lot; there is no
incremental format, and `LIMITATIONS.md` explains why shared blobs between slots
were rejected.

The 1 s and 5 s intervals committed zero checkpoints in these runs because the
workloads finish sooner. That is reported rather than hidden; a longer workload
is needed to characterise those intervals.
