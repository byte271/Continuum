class Accumulator:
    def __init__(self):
        self.value = 0

    def add(self, amount):
        self.value += amount


counter = Accumulator()
for value in [3, 5, 7]:
    counter.add(value)
print(counter.value)
