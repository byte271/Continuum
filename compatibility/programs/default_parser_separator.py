def parse_record(text, separator=":"):
    parts = text.split(separator)
    return (parts[0], int(parts[1]))


print(parse_record("items:42"))
