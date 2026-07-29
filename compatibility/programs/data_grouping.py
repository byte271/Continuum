records = [("east", 4), ("west", 2), ("east", 9), ("north", 3)]
groups = {}
for region, value in records:
    bucket = groups.setdefault(region, [])
    bucket.append(value)
print(groups)
