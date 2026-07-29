# Python language support

This matrix describes Continuum IR 0.2 as verified on CPython 3.12.13. The
status words are literal: **supported**, **partially supported**,
**explicitly rejected**, or **untested**. “Supported” applies only inside the
closed builtin, method, module, object, and resource model documented here.

## Core control flow

| Feature | Status | Verified boundary |
| --- | --- | --- |
| Name assignment | supported | RHS value and function-local binding survive continuation |
| Subscript assignment | supported | Python RHS-before-target evaluation order is tested |
| Chained assignment | explicitly rejected | Compiler diagnostic names `Assign` and chained assignment |
| Annotated assignment | explicitly rejected | `__annotations__` semantics are not implemented |
| Arithmetic and bitwise binary operators | supported | Operators listed in `BIN_OPS`; mixed-type edge cases follow host Python and are not exhaustively tested |
| Unary `+`, `-`, `not`, `~` | supported | Lowered to explicit unary IR |
| Single comparisons | supported | Equality, ordering, identity, membership |
| Chained comparisons | explicitly rejected | Single-evaluation chaining is not implemented |
| Boolean `and` / `or` | supported | Short-circuit value semantics are tested |
| `if` and conditional expressions | supported | Explicit branch PCs |
| `while`, nested loops | supported | Back-edge safe points; `else` supported |
| `for` over portable iterables | supported | `range`, list, tuple, string, bytes, dictionary |
| `break` / `continue` | supported | Iterator cleanup and continue-only freeze polling tested |
| `for/while ... else` | supported | Normal exhaustion and break paths tested |
| `return` | supported | Returned values are placed on the caller operand stack |
| Recursion | supported | Top-level recursive and mutually recursive function bindings tested |
| `match` | explicitly rejected | No IR lowering |

## Functions and names

| Feature | Status | Verified boundary |
| --- | --- | --- |
| Required positional parameters | supported | Argument count and binding validated |
| Keyword calls | supported | Named binding, duplicate, missing, and unknown checks |
| Default arguments | explicitly rejected | Definition-time default state not represented |
| Positional-only / keyword-only parameters | explicitly rejected | No calling convention support |
| `*args` / `**kwargs` parameters | explicitly rejected | No variadic frame binding |
| Starred / double-star call arguments | explicitly rejected | No expansion IR |
| Nested functions without captures | supported | Function value is stored in the enclosing local |
| Closures / free-variable capture | explicitly rejected | Compiler detects a read from an enclosing function scope |
| `nonlocal` / `global` | explicitly rejected | No closure cells or function writes to module bindings |
| Function objects in containers | supported | Portable `function_id` reference |
| Top-level recursive / mutually recursive functions | supported | Module binding lookup tested |
| Decorators | explicitly rejected | Definition-time transformation is not represented |
| Lambda | explicitly rejected | No `Lambda` lowering |

Function locals use Python's static-local rule: reading a name before its
function-local assignment raises an explicit `UnboundLocalError`; it does not
fall back to a same-named module global.

## Exceptions

| Feature | Status | Verified boundary |
| --- | --- | --- |
| `raise` of allowlisted built-in exceptions | supported | Exception object and arguments are graph encoded |
| Bare `raise` / `raise ... from` | explicitly rejected | Cause and active-handler semantics absent |
| `try/finally` | partially supported | Normal and exceptional pending reasons survive; return/break/continue out of `try` rejected |
| Checkpoint during pending propagation | supported | Freeze before finally body and resumed propagation tested |
| `try/except`, multiple handlers, `else` | explicitly rejected | Handler matching and traceback state absent |
| Exception raised inside `finally` | untested | Host exception unwinding is likely to replace the pending exception, but no claim is made |
| Tracebacks | explicitly rejected | Native traceback/frame objects are not portable values |

## Values and objects

| Feature | Status | Verified boundary |
| --- | --- | --- |
| `None`, bool, arbitrary integers | supported | Tagged values; integers use decimal strings |
| Finite and non-finite floats | supported | Hexadecimal text encoding |
| Unicode strings / bytes / bytearray | supported | UTF-8 JSON or base64 |
| Lists / dictionaries / tuples / sets / frozensets | supported | Graph records preserve supported contents |
| Shared mutable references | supported | Identity after restoration tested |
| Cyclic mutable graphs | supported | List/dictionary cycles tested |
| Cycles requiring an immutable/wrapper object first | explicitly rejected | Checkpoint preflight rejects before commit |
| User-defined classes and instances | explicitly rejected | No class layout or method model |
| Instance/class attributes | explicitly rejected | Attribute assignment is rejected; reads are restricted to portable bound methods/module attributes |
| Inheritance / class variables / self-referential instances | explicitly rejected | Classes are rejected |
| Arbitrary native-extension values | explicitly rejected at checkpoint | No type import or pickle fallback |

## Iteration and expressions

| Feature | Status | Verified boundary |
| --- | --- | --- |
| `range`, list, tuple, string, bytes iteration | supported | Iterator stores iterable and index |
| Dictionary iteration | partially supported | Key order/index preserved; changed key sequence raises; delete/reinsert with identical keys is not version-detected |
| Mutation of list during iteration | partially supported | Index-based semantics are deterministic but not exhaustively compared with CPython |
| Comprehensions / nested comprehensions | explicitly rejected | No comprehension frame/scoping model |
| Generator expressions / generators / `yield` | explicitly rejected | Generator suspension state is not represented |
| Slices and subscripting | supported | Three-part slices supported |
| F-strings | partially supported | Plain values and format specs; `!s`, `!r`, `!a` explicitly rejected |

## Context and resources

| Feature | Status | Verified boundary |
| --- | --- | --- |
| `with` / context managers | explicitly rejected | Enter/exit and exception-suppression state absent |
| Read-only regular files | supported | `r`, `rt`, `rb`; position and text options recorded |
| Multiple open files | supported | Two independent bundled offsets tested |
| Strict rebinding | supported | Path, size, mtime, SHA-256 |
| Relocate rebinding | supported | Mapped path, size, SHA-256; tested after original deletion |
| Bundle rebinding | supported | Original deletion and in-memory restoration tested |
| Missing/changed files | supported rejection | Resume aborts rather than using mismatched content |
| Writable files, pipes, sockets, devices | explicitly rejected | No portable commit/side-effect protocol |
| Target extraction conflicts | not applicable | Bundled resources are not extracted to filesystem paths |

## Safe points

Freeze polling occurs only at explicit `SAFEPOINT` instructions:

- after compiled statements;
- after iterator advancement and target binding, before a `for` body;
- before loop back edges and `continue` jumps;
- on entry to a `finally` body with its pending reason installed.

There is no freeze inside a host call, file operation, or individual IR
expression opcode. A caller operand stack may be partially populated while a
nested Continuum function is active; that stack is serialized and tested.
Requests arriving during an atomic host call are delayed to the next safe
point.
