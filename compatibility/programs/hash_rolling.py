text = "continuum"
value = 2166136261
for character in text.encode():
    value = ((value ^ character) * 16777619) % 4294967296
print(value)
