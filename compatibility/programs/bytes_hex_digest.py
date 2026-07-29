payload = "portable state".encode("utf-8")
pieces = [payload[:8], payload[8:]]
joined = b""
for piece in pieces:
    joined += piece
print(joined.hex(), joined.decode("utf-8"))
