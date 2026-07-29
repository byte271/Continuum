values = [1, 4, 2, 4, 3, 4, 2, 1]
histogram = {}
for value in values:
    histogram[value] = histogram.get(value, 0) + 1
print(histogram)
