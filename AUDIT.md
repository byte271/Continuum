# Implementation audit

Audited on 2026-07-29 using Linux x86_64 and CPython 3.12.13. The repository
was subsequently published and the cross-platform proof was completed at
commit `15bceefece050d06a1f504244a77434e31fd5228` in
[Actions run 30489463484](https://github.com/byte271/Continuum/actions/runs/30489463484).
The historical untouched-baseline note below describes the original imported
workspace, not the current repository identity.

## Untouched baseline

The first unmodified run did not reproduce the reported clean suite. The
process-independent test failed because the freeze client created the final
request path before writing JSON. The runtime observed an empty file and
reported:

```text
continuum: error: Expecting value: line 1 column 1 (char 0)
```

The raw environment and output are stored in
`artifacts/baseline/linux-x86_64-python3.12.13.txt`. The race was fixed by
fully writing and fsyncing a hidden same-directory file, then atomically
publishing it with a no-clobber hard link. The integration test passed five
consecutive runs after the fix.

## Execution path

| Stage | Preserved | Recreated | Recomputed | Unsupported / risk |
| --- | --- | --- | --- | --- |
| Source to AST | Source bytes and SHA-256 | CPython AST | Parsing | Unsupported nodes fail compilation |
| AST to IR | Logical operations, branches, function IDs, source lines, static local names | Instruction dictionaries | Compilation | This is lowering, not CPython bytecode/frame capture |
| Runtime | Frames, PCs, locals, operand stacks, block/finally records, globals | Decoded instruction cache | Host builtin/method results when originally called | Host calls are atomic; native live values can block freeze |
| Freeze request | Session/token/output request | Control-file paths | Liveness polling | Same-host filesystem control only |
| Graph capture | Reachable supported object graph, identity, cycles, RNG and resource references | Tagged graph document | Canonical set order key | Unsupported live values fail before destination creation |
| Resource capture | File mode/options, identity, offset, optional bytes | Metadata records | SHA-256 and bundled byte read | Read-only regular files only; bundle capture detects content change |
| Image commit | IR, source, graph, frames, resources, metadata | ZIP container/checksums | Hashes and compression | Directory fsync failure is currently ignored |
| Load | Exact stored documents | Validated in-memory documents | Checksums and cross-document comparisons | Integrity is not authenticity |
| Restore | Graph identity, frame order/PCs/stacks/locals/control state | Python containers, VM frames, file handles, decoded instruction cache | Target stdlib imports on later calls | Exact Python/runtime required; module implementation substitution remains possible |
| Resume | Next stored logical instruction | New OS process and target terminal attachment | Future program work only | No replay or source execution path exists in `resume` |

## Anti-fakery search and proof

The resume path is:

```text
CLI resume -> load_image -> ResourceManager.restore -> decode_graph
-> VirtualMachine.restore -> dispatch from stored frame PCs
```

It does not call `compile_source`, execute `code/program.py`, invoke the entry
function, or replay an event log. Searches found no filename, function-name,
sentinel, demo, skip-to-line, or application-counter special case in the
runtime.

`tests/test_adversarial_restart.py` adds a separate auditor process that
fsyncs action lines to an external log shared by source and target. It proves:

- the entry-module action appears once;
- three completed function-prologue actions appear once;
- completed loop action nonces are unique;
- the source exits before a separately created target process (numeric PIDs
  are recorded but may be reused);
- the resumed final hash equals an uninterrupted Continuum control.

The application source contains no Continuum import or checkpoint call.

## Confirmed defects fixed

1. Freeze request publication race exposed partial JSON.
2. Normal subscript assignment evaluated its target before its RHS, contrary
   to Python semantics.
3. Nested closures compiled but resolved captured names incorrectly.
4. Attribute assignment could create state silently omitted by specialized
   serializers.
5. A graph could be encoded and committed even when its wrapper cycle was not
   decodable.
6. Function locals read before assignment incorrectly fell back to globals.
7. `break` leaked a `for` iterator onto the operand stack.
8. `continue`-only loops could omit freeze polling.
9. Dictionary key mutation during iteration silently diverged from Python.
10. Malformed boolean graph values used truthiness coercion.
11. Resource records omitted reconstruction-field validation and duplicate-ID
    checks; partial restore could leak earlier handles.
12. Runtime, source, IR, capability, resource, and heap-count metadata were
    insufficiently cross-checked.
13. Failed freeze control files permanently blocked retry.
14. The reported performance ratio used `tracemalloc` only around the VM.

Closures, attribute assignment, annotated assignment, and f-string
conversions are now rejected during compilation rather than executed with
known-wrong semantics.

## Checkpoint boundary

Safe points are not arbitrary IR instruction boundaries. They occur after
statements, after `for` target binding, before loop back edges/continues, and
at finally-body entry. Operand stacks are serialized, so a caller may retain
a partially evaluated expression while a nested Continuum callee is frozen.

There is no checkpoint inside a host builtin, method, module call, file read,
or individual arithmetic opcode. A request at those points is delayed.

## Remaining high-risk gaps

- Only one native cross-platform pair and one proof workload have been
  verified. Broader program and resource portability remains unmeasured.
- Failure injection does not yet cover every ZIP write, fsync, checksum write,
  and rename boundary.
- `inspect` validates hashes and document schemas but does not fully decode the
  heap or execute IR; “integrity verified” is not a safety or authenticity
  statement.
- Dictionary delete/reinsert mutations that restore the identical key tuple
  are not version-detected.
- The allowlisted host-call surface is not a sandbox.
- Directory-fsync failure is ignored.
- No signatures, encryption, secret redaction, CPU/memory execution limits, or
  authenticated module provenance.
