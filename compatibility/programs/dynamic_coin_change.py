coins = [1, 3, 4]
target = 17
best = [target + 1] * (target + 1)
best[0] = 0
value = 1
while value <= target:
    for coin in coins:
        if coin <= value:
            best[value] = min(best[value], best[value - coin] + 1)
    value += 1
print(best[target])
