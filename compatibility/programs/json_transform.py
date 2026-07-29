import json

payload = json.loads('{"items":[3,1,4,1,5],"name":"sample"}')
payload["items"] = sorted(payload["items"])
payload["count"] = len(payload["items"])
print(json.dumps(payload))
