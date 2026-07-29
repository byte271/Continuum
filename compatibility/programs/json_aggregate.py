import json

records = json.loads('[{"team":"a","n":4},{"team":"b","n":2},{"team":"a","n":7}]')
totals = {}
for record in records:
    team = record["team"]
    totals[team] = totals.get(team, 0) + record["n"]
print(json.dumps(totals))
