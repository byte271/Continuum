trace = []
value = 7
try:
    trace.append("body")
    value *= 6
finally:
    trace.append("finally")
    value += 1
print(trace, value)
