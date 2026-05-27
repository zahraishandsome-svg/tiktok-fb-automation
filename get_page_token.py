"""
Use existing user_access_token from Walter Hayes source to get Ella Boyett page token.
"""
import requests
import json
from pathlib import Path
from src.facebook_poster import get_page_access_token

USER_TOKEN = "EAAqCAeD2BSgBRj0NTHdlfQbrPvt6elgCyjIOMHoQZA94kPpzQqmx8EBcfQMQfSZC0GJVSkjmNIb8HM8WwRDUzKPQ72jsP8glNFTo0loguIHTcpuuRWZCn17bgAezhTGHR77x96qF5u7wqrQHTXVky6WVnIxt7ZBgnb1NiPa3JD79aUhSWYu0C0STujQLlUcP"
ELLA_PAGE_ID = "107419715791454"

print("Fetching pages accessible to this user token...")
r = requests.get(
    "https://graph.facebook.com/v19.0/me/accounts",
    params={
        "access_token": USER_TOKEN,
        "fields": "id,name,access_token",
        "limit": 100,
    }
)
print(f"Status: {r.status_code}")
data = r.json()

if "error" in data:
    print(f"ERROR: {data['error']['message']}")
else:
    pages = data.get("data", [])
    print(f"Found {len(pages)} pages:")
    for p in pages:
        marker = " <-- ELLA BOYETT" if p["id"] == ELLA_PAGE_ID else ""
        print(f"  {p['id']} — {p['name']}{marker}")

    ella = next((p for p in pages if p["id"] == ELLA_PAGE_ID), None)
    if ella:
        print(f"\nGot page token for Ella Boyett!")
        token_data = {
            "page_access_token": ella["access_token"],
            "user_access_token": USER_TOKEN,
            "expires_at": "2026-07-23"
        }
        Path("tokens").mkdir(exist_ok=True)
        Path("tokens/page_1_token.json").write_text(
            json.dumps(token_data, indent=2)
        )
        print("Saved to tokens/page_1_token.json")
    else:
        print(f"\nElla Boyett page ({ELLA_PAGE_ID}) NOT found in this user's pages.")
        print("This user token doesn't have admin access to that page.")
