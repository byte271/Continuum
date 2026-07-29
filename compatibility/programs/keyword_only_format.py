def describe(value, *, prefix="value"):
    return prefix + "=" + str(value)


print(describe(12, prefix="count"))
