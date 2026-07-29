def combine(left, middle, right):
    return left * 100 + middle * 10 + right


values = [3, 4, 5]
print(combine(*values))
