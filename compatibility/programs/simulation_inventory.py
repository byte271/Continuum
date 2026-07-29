stock = {"a": 12, "b": 8, "c": 3}
orders = [("a", 2), ("b", 5), ("c", 4), ("a", 7)]
accepted = []
for name, count in orders:
    if stock[name] >= count:
        stock[name] -= count
        accepted.append((name, count))
print(stock, accepted)
