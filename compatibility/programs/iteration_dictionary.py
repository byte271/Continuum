mapping = {"alpha": 3, "beta": 5, "gamma": 7}
items = []
for key, value in mapping.items():
    items.append(key + ":" + str(value))
print("|".join(items))
