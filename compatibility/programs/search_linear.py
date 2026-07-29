values = [8, 6, 7, 5, 3, 0, 9]
target = 3
position = -1
index = 0
while index < len(values):
    if values[index] == target:
        position = index
        break
    index += 1
print(position)
