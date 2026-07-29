def search(values, index, current, output):
    if index == len(values):
        output.append(tuple(current))
        return
    search(values, index + 1, current, output)
    current.append(values[index])
    search(values, index + 1, current, output)
    current.pop()


answer = []
search([1, 2, 3, 4], 0, [], answer)
print(answer)
