text = "aaabccccdd"
result = []
index = 0
while index < len(text):
    end = index + 1
    while end < len(text) and text[end] == text[index]:
        end += 1
    result.append(text[index] + str(end - index))
    index = end
print("".join(result))
