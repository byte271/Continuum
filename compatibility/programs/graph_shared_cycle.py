shared = [1, 2, 3]
graph = {"left": shared, "right": shared}
graph["self"] = graph
graph["left"].append(4)
print(graph["left"] is graph["right"], graph["self"] is graph, len(shared))
