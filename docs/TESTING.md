# Test and claim matrix

| Required property | Automated evidence |
| --- | --- |
| Locals and nested frames | `test_nested_frames_locals_position_and_no_restart` |
| Current logical position | Combined source/target output equals control |
| Partial operand stack | `test_partial_caller_operand_stack_survives_nested_call` |
| Iterator advance/body boundary | `test_freeze_between_iterator_advance_and_loop_body` |
| No entry/prologue/loop replay | Three external-auditor tests in `test_adversarial_restart` |
| Lists, dictionaries, shared references, cycles | Continuation and graph-codec tests |
| Local-vs-global scope semantics | `test_function_local_does_not_fall_back_to_same_named_global` |
| Assignment evaluation order | `test_assignment_rhs_precedes_subscript_target_evaluation` |
| Loop cleanup/safe points/else | Semantic-audit tests |
| Exceptions / try-finally where supported | Normal and pending-exception continuation tests |
| Positional defaults | Definition-time, mutable-identity, keyword-binding, every-safe-point, and image tests |
| File offsets / multiple files | Resource bundle tests |
| Strict, relocate, bundle | Resource policy tests |
| Random state | Resource and codec tests |
| Module globals | Nested-frame continuation test |
| Corruption and truncation | Image tests |
| Noncanonical IDs, invalid refs, depth | Graph-codec hostile tests |
| Cross-document tampering | Altered IR and capability tests |
| Runtime incompatibility | Incompatible metadata test |
| Unsupported objects | Checkpoint rejection and retry tests |
| Final result equals uninterrupted run | Demo and adversarial final-hash tests |
| Demonstration cannot lose a freeze race on a fast host | `test_repeated_demos_never_lose_the_freeze_race`, `test_held_source_cannot_complete_before_release` |
| Demo synchronization stays out of `continuum run` | `test_start_gate_is_not_installed_outside_the_demo` |
| Linux x86_64 to macOS arm64, IR 0.2 | Actions run 30489463484; 26/26 proof conditions passed |
| Linux x86_64 to macOS arm64, IR 0.3 | Actions run 30509186641 at commit `3a4a43f` (runtime 0.2.0a1) and run 30585208329 at commit `a73073d` (runtime 0.2.0); both jobs passed |
| IR 0.4 language subset across platforms | Actions run 30592158078 at commit `21f7b2e`; class, instance, handler, variadic binding, and shared cell migrated |
| Native same-host continuation, three hosts | `runtime-bundles.yml` jobs `linux-x86_64`, `macos-arm64`, `windows-x86_64`, each running this complete suite |
| Windows x86_64 accepted, Windows arm64 rejected | `test_doctor_accepts_windows_x86_64`, `test_unsupported_windows_arm64_pair_is_rejected` |
| Windows proof markers under CRLF | `test_proof_markers_accept_windows_crlf_without_substring_matches` |
| Windows bundle determinism and installer contract | `test_windows_zip_builder_is_deterministic_and_normalizes_metadata`, `test_windows_source_builder_pins_exact_cpython_release` |
| Workflow shape for every native job | `test_runtime_bundle_workflow_has_all_native_jobs`, `test_two_native_jobs_and_evidence_transfer_are_fixed_in_workflow` |
| No Linux code/pointers | Archive payload test plus explicit JSON format |
| Source exits and target is new PID | Process-independent and adversarial tests |
| Idle safe-point cost/path | Signal publication-order and no-filesystem-poll tests |
| Deep non-executing verification | Invalid-graph, frame-metadata, and no-dispatch/no-recompile tests |
| Unchanged real-program corpus | 50-program CPython/uninterrupted/same-process/new-process differential runner |

## Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m compatibility.runner \
  --output compatibility/results/local.json
PYTHONPATH=. python3 benchmarks/measure.py \
  --iterations 10000 --repetitions 5
```

On Windows, use `python` and set `PYTHONPATH` separately:

```powershell
python -m unittest discover -s tests -v
python -m compatibility.runner --output compatibility\results\local.json
$env:PYTHONPATH = "."; python benchmarks\measure.py `
  --iterations 10000 --repetitions 5
```

The suite discovers 533 tests on every host. Skips are explicit and
mechanism-bound rather than platform exclusions:

| Host | Skipped |
| --- | --- |
| Linux x86_64 | native Apple Silicon test |
| macOS arm64 | native Apple Silicon test, unless `CONTINUUM_LINUX_IMAGE` supplies a qualified Linux image |
| Windows x86_64 | native Apple Silicon test, two POSIX signal-notification tests, the POSIX shell-installer test, the POSIX symlink-launcher test |

On every host the static undefined-name gate also skips unless `pylint` is
importable. It is not a runtime dependency; the stress workflow installs it, so
that job is the one where the gate actually runs.

Corpus and benchmark samples are under their respective `results/` directories.
The published corpus and benchmark numbers are Linux x86_64 measurements. Raw
untouched-baseline and final proof logs are attached to the Actions runs cited
in STATUS.md rather than committed to the tree.

## Real cross-platform protocol

Use `validation/cross_platform/README.md`. The complete workflow passed in
[Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484)
at commit `15bceefece050d06a1f504244a77434e31fd5228`. The source
ran in a native x86_64 Linux GitHub-hosted VM and the unchanged image resumed
in a new native process on a GitHub-hosted Apple Silicon macOS arm64 runner.
All 26 proof conditions and the final evidence manifest passed.

That run proves IR 0.2/runtime 0.1.1.dev0. The same workflow has since passed
for IR 0.3/runtime 0.2.0a1 in
[Actions run 30509186641](https://github.com/byte271/Continuum/actions/runs/30509186641)
at commit `3a4a43fb74331113225d7b9a3a0fef4afd1371fa`, with a freshly generated
IR 0.3 image. It runs on every push to `main`.

Both results cover one direction only. The workflow has two jobs,
`linux-source` and `macos-target`, and the validation package contains only
`source_linux.py` and `target_macos.py`. There is no Windows leg, so no
cross-platform claim involving Windows is available from any test in this
repository. Adding one is a separate milestone in `ROADMAP.md`.

Passing only manifest checks, containers, emulation, mocked platform strings,
or this Linux dry run does not satisfy cross-platform acceptance. Neither does
passing this suite natively on a host: that is same-host evidence.

## Rolling checkpoint tests

Five modules, 113 checkpoint tests in total:

| Module | Covers |
| --- | --- |
| `test_checkpoint_store.py` | interval and slot parsing, rotation, generation monotonicity, selection that ignores timestamps, corruption fallback, lineage isolation, durability contract, scheduler coalescing and failure policy, non-terminating execution |
| `test_checkpoint_crash_injection.py` | a real raised failure at each of the seven commit stages, at first-commit and steady state, plus a genuine mid-serialization failure |
| `test_checkpoint_cli.py` | new commands, JSON output, and that no pre-existing `run` invocation changes meaning |
| `test_checkpoint_process_crash.py` | end-to-end: a real process is `SIGKILL`ed (`Popen.kill()` on Windows), confirmed dead, and recovered in a separate process |
| `test_checkpoint_audit.py` | the PR review findings: capture-failure conversion under both policies, published-generation reuse, directory ownership, bounded history, complete pause measurement, recovery refusal categories, errno guards, ASCII lineage, commit-path validation cost, and format compatibility |

The crash-injection tests do not mock success. Each raises a real exception at
one commit stage, leaves whatever partial state that produces, and then reads
the directory back through the ordinary image reader. The invariant asserted
after every injection is the same: recovery selects the last fully committed
generation or the newly committed one, never a partial file.

`test_checkpoint_process_crash.py` writes child output to files rather than
pipes. The workload outpaces any reader, and a full pipe would block the child
mid-run, stalling the very checkpoints under test. Its barrier polls committed
on-disk state rather than sleeping a fixed time.

Only `test_checkpoint_process_crash.py` needs a verified interpreter (3.12.13 or
3.13.14), because it drives `continuum run` and `continuum recover` as
subprocesses and those refuse unverified interpreters. The other four modules
run anywhere.

**No test uses a fixed sleep as proof of synchronization.** The scheduler takes
an injected clock, so "a checkpoint happens every N safe points" is a property
of the program rather than of how fast the host is -- an earlier version of
these tests depended on the workload outlasting a real 1ms interval and failed
on the Windows runner, which finished the program first. The end-to-end crash
test blocks on committed on-disk generation, not on elapsed time.
