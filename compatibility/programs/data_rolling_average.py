values = [10, 20, 30, 40, 50, 60]
window = 3
averages = []
index = 0
while index + window <= len(values):
    averages.append(sum(values[index : index + window]) / window)
    index += 1
print(averages)
