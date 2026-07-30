import hashlib
import random


class Ledger:
    """VM-owned class: instance attributes must survive the migration."""

    scale = 3

    def __init__(self, label):
        self.label = label
        self.entries = []
        self.rejected = 0

    def record(self, value, *tags, weight=1, **extra):
        # Exercises positional, variadic, keyword-only and **kwargs binding
        # inside a frame that is live when the checkpoint is taken.
        self.entries.append((value * weight + len(tags) + len(extra)) % 1000003)
        return len(self.entries)

    def total(self):
        running = 0
        for entry in self.entries:
            running = (running + entry) % 1000003
        return running


def make_counter():
    """Closure whose cell must stay shared through the image."""

    count = 0

    def bump(step):
        nonlocal count
        count = count + step
        return count

    def peek():
        return count

    return [bump, peek]


def inner_step(index, graph, big, rng, handle, nonce, ledger, counter):
    print("ACTION", nonce, "ITER", index, flush=True)
    line = handle.readline()
    if line == "":
        handle.seek(0)
        line = handle.readline()
    salt = rng.randint(1, 1000000)
    value = (big[index % len(big)] + salt + len(line)) % 1000000007
    spin = 0
    mixed = value
    while spin < 80:
        mixed = (mixed * 1664525 + spin + 1013904223) % 4294967296
        spin += 1
    graph["left"].append(mixed)
    # A handler frame and a caught exception, exercised on a fixed schedule so
    # both hosts follow the same path.
    try:
        if index % 7 == 3:
            raise ValueError(str(index))
        ledger.record(mixed, "plain", weight=2, source="loop")
    except ValueError as error:
        ledger.rejected = ledger.rejected + len(str(error))
        ledger.record(mixed, "recovered", "handler", weight=1)
    counter[0](1)
    return mixed


def middle_layer(total, graph, big, rng, handle, nonce, ledger, counter):
    print("ACTION", nonce, "PROLOGUE_MIDDLE", flush=True)
    index = 0
    aggregate = 0
    while index < total:
        aggregate += inner_step(
            index, graph, big, rng, handle, nonce, ledger, counter
        )
        if index % 10 == 0:
            print(
                "Processing " + format(index * 100.0 / total, ".1f") + "%",
                flush=True,
            )
        index += 1
    return aggregate


def workload(path, total, nonce):
    print("ACTION", nonce, "PROLOGUE_WORKLOAD", flush=True)
    shared = []
    graph = {"left": shared, "right": shared}
    graph["self"] = graph
    big = {}
    build_index = 0
    while build_index < 5000:
        big[build_index] = build_index * build_index
        build_index += 1
    rng = random.Random(20260729)
    ledger = Ledger("proof")
    counter = make_counter()
    handle = open(path, "r", encoding="utf-8")
    prefix = handle.read(11)
    final_offset = 0
    try:
        aggregate = middle_layer(
            total, graph, big, rng, handle, nonce, ledger, counter
        )
    finally:
        final_offset = handle.tell()
        handle.close()
    proof = (
        str(aggregate)
        + ":"
        + str(len(big))
        + ":"
        + str(len(graph["left"]))
        + ":"
        + str(final_offset)
        + ":"
        + prefix
        + ":"
        + str(ledger.total())
        + ":"
        + str(len(ledger.entries))
        + ":"
        + str(ledger.rejected)
        + ":"
        + str(ledger.label)
        + ":"
        + str(Ledger.scale)
        + ":"
        + str(counter[1]())
    )
    digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    print("IDENTITY", graph["left"] is graph["right"], graph["self"] is graph)
    print("FINAL", digest, flush=True)
    return digest


input_path = __args__[1]
iteration_count = int(__args__[2])
proof_nonce = __args__[3]
print("ACTION", proof_nonce, "ENTRY", flush=True)
workload(input_path, iteration_count, proof_nonce)
