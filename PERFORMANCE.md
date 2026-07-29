# Performance

Correctness remains the priority. These are raw local measurements from one
Linux x86_64 environment, not portable targets.

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

The portable image and IR are unchanged except for the intentional IR 0.2
semantic version bump. The runtime cache is recreated after restore.

Post-optimization profile:

```bash
PYTHONPATH=. python3 -m benchmarks.profile_runtime \
  --iterations 20000 \
  --profile benchmarks/results/runtime-2026-07-29-optimized.prof \
  --report benchmarks/results/runtime-2026-07-29-optimized.txt
```

The same profiled workload fell from 1.602 s to 1.196 s (25.3%). The largest
remaining region is `_execute`: 0.454 s self-time and 0.822 s cumulative.
Safe-point request-path polling is separately large in CLI operation because
it performs a filesystem existence check at every safe point.

No native extension, JIT, or architecture-specific acceleration was added.
