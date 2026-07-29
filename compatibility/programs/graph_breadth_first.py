graph = {"a": ["b", "c"], "b": ["d"], "c": ["e"], "d": [], "e": []}
queue = ["a"]
seen = set()
order = []
while queue:
    node = queue.pop(0)
    if node not in seen:
        seen.add(node)
        order.append(node)
        queue.extend(graph[node])
print(order)
