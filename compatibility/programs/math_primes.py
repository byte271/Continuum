primes = []
for value in range(2, 60):
    prime = True
    for candidate in range(2, value):
        if value % candidate == 0:
            prime = False
            break
    if prime:
        primes.append(value)
print(primes)
