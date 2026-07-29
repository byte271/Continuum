def fibonacci(value, memo={}):
    if value in memo:
        return memo[value]
    if value < 2:
        return value
    memo[value] = fibonacci(value - 1, memo) + fibonacci(value - 2, memo)
    return memo[value]


print(fibonacci(16))
