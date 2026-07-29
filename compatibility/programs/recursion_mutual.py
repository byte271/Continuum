def is_even(value):
    if value == 0:
        return True
    return is_odd(value - 1)


def is_odd(value):
    if value == 0:
        return False
    return is_even(value - 1)


print(is_even(20), is_odd(21))
