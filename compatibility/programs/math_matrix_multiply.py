left = [[1, 2, 3], [4, 5, 6]]
right = [[7, 8], [9, 10], [11, 12]]
result = []
row_index = 0
while row_index < len(left):
    row = []
    column_index = 0
    while column_index < len(right[0]):
        total = 0
        inner = 0
        while inner < len(right):
            total += left[row_index][inner] * right[inner][column_index]
            inner += 1
        row.append(total)
        column_index += 1
    result.append(row)
    row_index += 1
print(result)
