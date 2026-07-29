left = "portable"
right = "continuable"
table = []
row = 0
while row <= len(left):
    table.append([0] * (len(right) + 1))
    row += 1
row = 1
while row <= len(left):
    column = 1
    while column <= len(right):
        if left[row - 1] == right[column - 1]:
            table[row][column] = table[row - 1][column - 1] + 1
        else:
            table[row][column] = max(
                table[row - 1][column], table[row][column - 1]
            )
        column += 1
    row += 1
print(table[len(left)][len(right)])
