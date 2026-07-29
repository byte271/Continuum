graph = {"a": ["b", "c"], "b": ["d"], "c": ["d", "e"], "d": [], "e": []}
stack = ["a"]
seen = set()
order = []
while stack:
    node = stack.pop()
    if node not in seen:
        seen.add(node)
        order.append(node)
        neighbours = graph[node][:]
        neighbours.reverse()
        stack.extend(neighbours)
print(order)
