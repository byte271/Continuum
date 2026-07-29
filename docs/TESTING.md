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
| Linux x86_64 to macOS arm64 | Manual test/package only; skipped/unverified |
| No Linux code/pointers | Archive payload test plus explicit JSON format |
| Source exits and target is new PID | Process-independent and adversarial tests |

## Commands

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 benchmarks/measure.py \
  --iterations 10000 --repetitions 5
```

Raw untouched-baseline and final verification logs are under `artifacts/`.

## Real cross-platform protocol

Use `validation/cross_platform/README.md`. The Linux script has been exercised
on the current x86_64 environment, including bundled-input deletion and a
same-host new-process control comparison. The macOS script has not run because
no real Apple Silicon host is available.

Passing only manifest checks, containers, emulation, mocked platform strings,
or this Linux dry run does not satisfy cross-platform acceptance.
