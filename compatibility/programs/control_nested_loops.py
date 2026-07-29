pairs = []
left = 0
while left < 8:
    right = 0
    while right < 8:
        if (left + right) % 3 == 0:
            right += 1
            continue
        if left * right > 24:
            break
        pairs.append((left, right))
        right += 1
    left += 1
print(len(pairs), pairs[-1])
