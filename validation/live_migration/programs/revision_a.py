import random


class Tally:
    def __init__(self, seed):
        self.total = seed

    def add(self, value):
        self.total = self.total + value


def make_bias(base):
    def bias(value):
        return value + base
    return bias


def leaf(limit, tally, bias, graph):
    index = 0
    while index < limit:
        tally.add(bias(index))
        graph["shared"].append(index)
        print(f"ACTION {index} {tally.total} {random.randint(0, 999)}")
        index += 1
    return tally.total


def middle(limit, tally, bias, graph):
    return leaf(limit, tally, bias, graph)


def outer(limit):
    random.seed(20260731)
    shared = []
    graph = {"left": shared, "right": shared, "shared": shared}
    graph["self"] = graph
    tally = Tally(7)
    bias = make_bias(3)
    answer = middle(limit, tally, bias, graph)
    print(f"FINAL {answer} {len(shared)}")
    return answer


result = outer(30)
