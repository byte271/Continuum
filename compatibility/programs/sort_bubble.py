values = [9, 1, 8, 2, 7, 3, 6, 4, 5]
end = len(values)
while end > 1:
    index = 1
    while index < end:
        if values[index] < values[index - 1]:
            saved = values[index]
            values[index] = values[index - 1]
            values[index - 1] = saved
        index += 1
    end -= 1
print(values)
