values = [12, 7, 9, 15, 11, 8]
ordered = sorted(values)
mean = sum(values) / len(values)
middle = len(ordered) // 2
median = (ordered[middle - 1] + ordered[middle]) / 2
print(round(mean, 3), median, min(values), max(values))
