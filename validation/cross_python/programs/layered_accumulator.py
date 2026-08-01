class Accumulator:
    def __init__(self, seed):
        self.total = seed

    def add(self, value):
        self.total = self.total + value


def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, accumulator, bias, graph):
    index = 0
    while index < limit:
        value = bias(index)
        accumulator.add(value)
        graph["shared"].append(value)
        print(f"ACTION {index} {accumulator.total}")
        index += 1
    return accumulator.total


def middle(limit, accumulator, bias, graph):
    return leaf(limit, accumulator, bias, graph)


def outer(limit):
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    accumulator = Accumulator(7)
    bias = make_bias(3)
    answer = middle(limit, accumulator, bias, graph)
    print(f"FINAL {answer} {len(shared)}")
    return answer


result = outer(400)
