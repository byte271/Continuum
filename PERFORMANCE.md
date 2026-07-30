# Performance

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
Current development uses IR 0.3 for positional default-argument state. Runtime
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
