def clamp(value, lower=0, upper=100):
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


print(clamp(-4), clamp(17), clamp(190))
