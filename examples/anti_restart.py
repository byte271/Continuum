def leaf(limit, values):
    index = 0
    while index < limit:
        values.append(index)
        index += 1
    return len(values)


def middle(limit, values):
    return leaf(limit, values)


def outer(limit):
    values = []
    print("START_SENTINEL", flush=True)
    result = middle(limit, values)
    print("FINAL_COUNT", result, flush=True)
    return result


outer(int(__args__[1]))

