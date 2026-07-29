text = "(()(()))()"
depth = 0
valid = True
for character in text:
    if character == "(":
        depth += 1
    else:
        depth -= 1
    if depth < 0:
        valid = False
print(valid and depth == 0)
