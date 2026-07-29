text = "red blue red green blue red"
counts = {}
for word in text.split():
    counts[word] = counts.get(word, 0) + 1
print(counts)
