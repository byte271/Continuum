values = [5, 2, 9, 1, 5, 6]
index = 1
while index < len(values):
    value = values[index]
    position = index - 1
    while position >= 0 and values[position] > value:
        values[position + 1] = values[position]
        position -= 1
    values[position + 1] = value
    index += 1
print(values)
