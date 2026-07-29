values = [2, 5, 7, 11, 13, 17, 19, 23]
target = 17
left = 0
right = len(values) - 1
found = -1
while left <= right:
    middle = (left + right) // 2
    if values[middle] == target:
        found = middle
        break
    if values[middle] < target:
        left = middle + 1
    else:
        right = middle - 1
print(found)
