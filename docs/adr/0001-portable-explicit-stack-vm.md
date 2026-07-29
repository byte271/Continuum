# ADR 0001: Use a portable explicit-stack execution layer

- Status: accepted for v0.1
- Date: 2026-07-29

## Decision

Compile a validated Python source subset into a small, platform-independent
stack-machine IR. Execute it with explicit frames and serialize those frames
plus a safe graph-coded heap. Use explicit resource rebinding for regular
files.

This is a hybrid of approaches 5 and 6 below, with the Python AST used only as
the front end. It was selected because it is the smallest architecture that
can demonstrate a genuine nested continuation without serializing native
pointers, replaying prior work, or requiring application checkpoint code.

## Strategies evaluated

| Strategy | Can be genuine? | Main issue for the first proof | Decision |
| --- | --- | --- | --- |
| Controlled CPython fork | Yes | Private, release-specific frame/evaluator internals; heap and active C state still need portable translation | Defer |
| CPython bytecode transformation | Yes | Bytecode, specialization, exception tables, and stack effects are version-specific; transformation correctness is broad | Possible later |
| AST state-machine transformation | Yes | Merely inserting yields is insufficient because generator frames are not serializable; full expression/call lowering is still needed | Use AST only as front end |
| Stackless-style runtime | Yes | Strong precedent, but requires a maintained interpreter fork and has C-stack/restorability constraints | Reuse concepts |
| Custom bytecode interpreter | Yes | Narrow Python compatibility and high slowdown | Select |
| Transformed continuation + graph serialization | Yes | Must define a strict supported object/resource set | Select |

## Evidence

### CPython frames

The public CPython frame API describes `PyFrameObject` as opaque, and Python
3.11 removed its public members. The 3.11 release notes also explain that
ordinary calls often avoid materializing old-style frame objects. Private
`_PyInterpreterFrame` fields include interpreter-specific instruction and
stack state; treating that layout as a portable ABI would be false.

- <https://docs.python.org/3/c-api/frame.html>
- <https://docs.python.org/3/whatsnew/3.11.html>
- <https://github.com/python/cpython/blob/v3.12.13/Include/internal/pycore_frame.h>
- <https://docs.python.org/3/c-api/code.html>

General-purpose Python serializers do not solve this. Dill's own documentation
lists frame, generator, and traceback among types it cannot pickle.

- <https://dill.readthedocs.io/>

### Stackless Python

Stackless demonstrates that this capability is real when the language runtime
owns restorable tasklet state. Its documentation explicitly shows a tasklet
being serialized mid-execution and resumed, and describes the pickle form as
platform independent. It also warns that tasklets involving lost C-stack
runtime state are not runnable after unpickling.

- <https://stackless.readthedocs.io/en/3.7-slp/library/stackless/pickling.html>
- <https://stackless.readthedocs.io/en/3.7-slp/library/stackless/tasklets.html>
- <https://stackless.readthedocs.io/en/3.8-slp/c-api/stackless.html>

The lesson used here is to keep the resumable Python call stack separate from
the host C stack.

### CRIU and DMTCP

CRIU and DMTCP prove robust same-environment process checkpointing and provide
useful concepts: quiescence, resource inventories, atomic images, and explicit
compatibility. Their process-memory approach preserves an OS/ABI-specific
address space and kernel resources; it does not provide a portable
Python-object and instruction representation for Linux x86_64 to macOS ARM64.

- <https://criu.org/Main_Page>
- <https://criu.org/Comparison_to_other_CR_projects>
- <https://dmtcp.github.io/>
- <https://arxiv.org/abs/cs/0701037>

### WebAssembly

WebAssembly validates the use of a portable stack-based abstract machine, but
a normal Wasm engine's live host/runtime call stack is not automatically a
standard checkpoint format. Continuum borrows the portable IR and validated
stack-machine idea, not a claim that existing Wasm snapshots solve the Python
heap and resources.

- <https://webassembly.org/>
- <https://github.com/WebAssembly/design/blob/main/Rationale.md>

## Reality of the continuation

The source runtime may freeze with several user functions active. Caller
frames can hold values on their operand stacks while a callee is suspended.
The image stores every frame's next `pc`. Resume constructs a new VM directly
from this state and enters the dispatch loop. It does not execute module
initialization, call the entry function, or replay an event log.

The original anti-restart test concatenates source and target output and
compares it with an uninterrupted control. The audited suite adds a separate
process that fsyncs action nonces to an external log; entry, function prologue,
and completed loop actions must each remain unique across source and target.

## Consequences

Positive:

- no native pointers, code pages, or virtual addresses in the image;
- continuation and heap formats can be inspected;
- unsupported syntax and values fail explicitly;
- a new process can resume without source replay;
- cross-architecture and cross-OS restoration is technically plausible.

Negative:

- this is a Python subset, not transparent CPython compatibility;
- runtime slowdown is currently large;
- library calls are atomic regions and cannot freeze internally;
- native-returned objects must be gone at a safe point or checkpoint fails;
- cross-platform behavior still requires native Apple Silicon verification.
