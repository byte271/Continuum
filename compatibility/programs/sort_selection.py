values = [64, 25, 12, 22, 11]
left = 0
while left < len(values):
    smallest = left
    right = left + 1
    while right < len(values):
        if values[right] < values[smallest]:
            smallest = right
        right += 1
    saved = values[left]
    values[left] = values[smallest]
    values[smallest] = saved
    left += 1
print(values)
