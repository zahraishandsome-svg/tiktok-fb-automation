"""
Check how many videos are on the Ella Boyett Facebook page
and try different pagination strategies to get them all.
"""
import requests
import json
import time

PAGE_ID = "107419715791454"
TOKEN = json.load(open("tokens/page_1_token.json"))["page_access_token"]
GRAPH = "https://graph.facebook.com/v19.0"


def fetch_all_videos_small_pages(limit=25):
    """Fetch all videos using small page size to avoid 500s."""
    videos = []
    url = f"{GRAPH}/{PAGE_ID}/videos"
    params = {
        "fields": "id,title,description,created_time",
        "access_token": TOKEN,
        "limit": limit,
    }
    page_num = 0
    while url:
        page_num += 1
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 500:
            print(f"  500 error on page {page_num} after {len(videos)} videos — stopping")
            break
        if not resp.ok:
            print(f"  HTTP {resp.status_code} on page {page_num}: {resp.text[:200]}")
            break
        data = resp.json()
        batch = data.get("data", [])
        videos.extend(batch)
        print(f"  Page {page_num}: +{len(batch)} videos (total: {len(videos)})")
        next_page = data.get("paging", {}).get("next")
        url = next_page
        params = {}
        if next_page:
            time.sleep(0.5)  # small delay to avoid rate limiting
    return videos


def fetch_published_videos(limit=25):
    """Try published_videos endpoint instead of videos."""
    videos = []
    url = f"{GRAPH}/{PAGE_ID}/published_posts"
    # This won't work for videos specifically, try video_posts
    url = f"{GRAPH}/{PAGE_ID}/video_posts"
    params = {
        "fields": "id,message,created_time",
        "access_token": TOKEN,
        "limit": limit,
    }
    page_num = 0
    while url and page_num < 5:  # just test first 5 pages
        page_num += 1
        resp = requests.get(url, params=params, timeout=30)
        if not resp.ok:
            print(f"  video_posts HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        batch = data.get("data", [])
        videos.extend(batch)
        print(f"  video_posts page {page_num}: +{len(batch)}")
        url = data.get("paging", {}).get("next")
        params = {}
    return videos


print("=== Strategy 1: /videos with limit=25 ===")
vids = fetch_all_videos_small_pages(limit=25)
print(f"\nTotal fetched: {len(vids)}")
print(f"Titles sample:")
for v in vids[:5]:
    print(f"  [{v.get('id')}] {v.get('title', '(no title)')[:60]}")
