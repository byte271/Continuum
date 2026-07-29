import hashlib
import random


def inner_step(index, graph, big, rng, handle):
    line = handle.readline()
    if line == "":
        handle.seek(0)
        line = handle.readline()
    salt = rng.randint(1, 1000000)
    value = (big[index % len(big)] + salt + len(line)) % 1000000007
    graph["left"].append(value)
    return value


def middle_layer(total, graph, big, rng, handle):
    index = 0
    aggregate = 0
    while index < total:
        aggregate += inner_step(index, graph, big, rng, handle)
        if index % 100 == 0:
            percent = index * 100.0 / total
            print("Processing " + format(percent, ".1f") + "%", flush=True)
        index += 1
    return aggregate


def workload(path, total):
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
        aggregate = middle_layer(total, graph, big, rng, handle)
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
    print("FINAL", digest)
    return digest


input_path = __args__[1]
iteration_count = int(__args__[2])
workload(input_path, iteration_count)

