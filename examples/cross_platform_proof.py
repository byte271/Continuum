import hashlib
import random


def inner_step(index, graph, big, rng, handle, nonce):
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
    return mixed


def middle_layer(total, graph, big, rng, handle, nonce):
    index = 0
    aggregate = 0
    while index < total:
        aggregate += inner_step(index, graph, big, rng, handle, nonce)
        if index % 10 == 0:
            print(
                "Processing " + format(index * 100.0 / total, ".1f") + "%",
                flush=True,
            )
        index += 1
    return aggregate


def workload(path, total, nonce):
    print("ACTION", nonce, "ENTRY", flush=True)
    shared = []
    graph = {"left": shared, "right": shared}
    graph["self"] = graph
    big = {}
    build_index = 0
    while build_index < 5000:
        big[build_index] = build_index * build_index
        build_index += 1
    rng = random.Random(20260729)
    handle = open(path, "r", encoding="utf-8")
    prefix = handle.read(11)
    final_offset = 0
    try:
        aggregate = middle_layer(total, graph, big, rng, handle, nonce)
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
    )
    digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    print("IDENTITY", graph["left"] is graph["right"], graph["self"] is graph)
    print("FINAL", digest, flush=True)
    return digest


input_path = __args__[1]
iteration_count = int(__args__[2])
proof_nonce = __args__[3]
workload(input_path, iteration_count, proof_nonce)
