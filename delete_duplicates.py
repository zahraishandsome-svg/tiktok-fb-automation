"""Delete duplicate Facebook videos."""
import json, urllib.request

token_file = "tokens/page_1_token.json"
with open(token_file) as f:
    token = json.load(f)["page_access_token"]

to_delete = ["2766565787038893", "976074761944437"]

for vid_id in to_delete:
    url = f"https://graph.facebook.com/v19.0/{vid_id}?access_token={token}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
            print(f"Deleted {vid_id}: {result}")
    except urllib.error.HTTPError as e:
        print(f"Failed to delete {vid_id}: HTTP {e.code} — {e.read().decode()}")
