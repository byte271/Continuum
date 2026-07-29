import random

rng = random.Random(20260729)
position = 0
path = []
for index in range(40):
    if rng.randint(0, 1) == 0:
        position -= 1
    else:
        position += 1
    path.append(position)
print(position, path)
