def fibonacci(value):
    if value < 2:
        return value
    return fibonacci(value - 1) + fibonacci(value - 2)


print(fibonacci(12))
