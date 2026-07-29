import hashlib

digest = hashlib.sha256()
for chunk in [b"portable", b"-", b"continuation"]:
    digest.update(chunk)
print(digest.hexdigest())
