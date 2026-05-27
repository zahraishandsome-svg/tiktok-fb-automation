"""Find duplicate videos on the Facebook page posted today."""
import json, urllib.request, urllib.parse
from datetime import datetime, timezone

token_file = "tokens/page_1_token.json"
creds_file = "credentials/page_1_credentials.json"

with open(token_file) as f:
    token = json.load(f)["page_access_token"]
with open(creds_file) as f:
    page_id = json.load(f)["page_id"]

# Fetch last 25 videos from the page
url = (f"https://graph.facebook.com/v19.0/{page_id}/videos"
       f"?fields=id,title,description,created_time"
       f"&limit=25&access_token={token}")

req = urllib.request.Request(url)
with urllib.request.urlopen(req) as r:
    data = json.loads(r.read())

videos = data.get("data", [])
print(f"Last {len(videos)} videos on page:\n")

today = datetime.now(timezone.utc).date().isoformat()
from collections import defaultdict
by_title = defaultdict(list)

for v in videos:
    created = v.get("created_time", "")[:10]
    title = (v.get("title") or v.get("description") or "")[:60]
    print(f"  {v['id']} | {created} | {title}")
    if created == today:
        by_title[title].append(v["id"])

print("\nToday's duplicates:")
for title, ids in by_title.items():
    if len(ids) > 1:
        print(f"  '{title}' — {len(ids)} copies: {ids}")
        print(f"  Keep: {ids[-1]}  |  Delete: {ids[:-1]}")
