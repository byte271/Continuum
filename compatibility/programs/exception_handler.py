values = ["12", "bad", "7"]
parsed = []
for value in values:
    try:
        parsed.append(int(value))
    except ValueError:
        parsed.append(-1)
print(parsed)
