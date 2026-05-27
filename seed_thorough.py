"""
Thorough seed: matches TikTok videos to FB posts using multiple strategies:
  1. Title match (exact normalized)
  2. Description match (first 80 chars normalized)
  3. Date proximity match (TikTok timestamp within 3 days of FB created_time)

Marks all matched TikTok videos as 'uploaded' in DB so they're never reposted.
"""
import json
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from src.config import load_config, load_credentials, load_token
from src.db import init_db, get_connection
from src.tiktok_downloader import get_profile_videos
from src.facebook_poster import GRAPH_URL

PAGE_ID_OVERRIDE = None  # set to override credentials page_id


def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[\U00010000-\U0010ffff\U0001F600-\U0001F64F]', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_fb_time(s: str) -> datetime:
    """Parse Facebook created_time to UTC datetime."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S+0000").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_all_fb_videos(page_id: str, token: str) -> list:
    """Fetch all videos from FB page using small batches to avoid 500s."""
    videos = []
    url = f"{GRAPH_URL}/{page_id}/videos"
    params = {
        "fields": "id,title,description,created_time",
        "access_token": token,
        "limit": 25,
    }
    page_num = 0
    while url:
        page_num += 1
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 500:
            print(f"  [FB] Page {page_num}: 500 server error — stopped at {len(videos)} videos")
            break
        if not resp.ok:
            print(f"  [FB] Page {page_num}: HTTP {resp.status_code} — {resp.text[:100]}")
            break
        data = resp.json()
        batch = data.get("data", [])
        videos.extend(batch)
        print(f"  [FB] Page {page_num}: +{len(batch)} (total {len(videos)})")
        url = data.get("paging", {}).get("next")
        params = {}
        if url:
            time.sleep(0.4)
    return videos


def main():
    dry_run = "--dry-run" in sys.argv
    channel_id = "page_1"

    config = load_config()
    channels = {ch["id"]: ch for ch in config["channels"]}
    channel = channels[channel_id]
    init_db()

    creds = load_credentials(channel["credentials_file"])
    token_data = load_token(channel["token_file"])
    page_id = creds["page_id"]
    page_token = token_data["page_access_token"]

    print(f"Fetching ALL videos from Facebook Page {page_id}...")
    fb_videos = fetch_all_fb_videos(page_id, page_token)
    print(f"=> {len(fb_videos)} FB videos fetched\n")

    print(f"Fetching TikTok profile: @{channel['tiktok_username']}...")
    tiktok_videos = get_profile_videos(channel["tiktok_username"])
    if tiktok_videos is None:
        print("ERROR: Could not fetch TikTok profile")
        sys.exit(1)
    print(f"=> {len(tiktok_videos)} TikTok videos fetched\n")

    # Build FB lookup indexes
    fb_by_title = {}
    fb_by_desc = {}
    fb_by_date = []  # list of (datetime, fb_video)

    for v in fb_videos:
        title_norm = normalize(v.get("title") or "")
        desc_norm = normalize((v.get("description") or "")[:80])
        dt = parse_fb_time(v.get("created_time") or "")

        if title_norm and len(title_norm) > 3:
            fb_by_title[title_norm] = v
        if desc_norm and len(desc_norm) > 10:
            fb_by_desc[desc_norm] = v
        if dt:
            fb_by_date.append((dt, v))

    fb_by_date.sort(key=lambda x: x[0])

    print(f"FB index: {len(fb_by_title)} with title, {len(fb_by_desc)} with description, {len(fb_by_date)} with date\n")

    matched = 0
    matched_ids = set()
    conn = get_connection()
    match_log = []

    for tk in tiktok_videos:
        tk_title_norm = normalize(tk.get("title") or "")
        tk_desc_norm = normalize((tk.get("description") or tk.get("title") or "")[:80])
        tk_ts = tk.get("timestamp")
        tk_dt = datetime.fromtimestamp(int(tk_ts), tz=timezone.utc) if tk_ts else None

        fb_match = None
        match_reason = ""

        # Strategy 1: title match
        if tk_title_norm and len(tk_title_norm) > 3:
            fb_match = fb_by_title.get(tk_title_norm)
            if fb_match:
                match_reason = "title"

        # Strategy 2: description match (first 80 chars)
        if not fb_match and tk_desc_norm and len(tk_desc_norm) > 10:
            fb_match = fb_by_desc.get(tk_desc_norm)
            if fb_match:
                match_reason = "description"

        # Strategy 3: date proximity (within 1 day)
        if not fb_match and tk_dt and fb_by_date:
            window = timedelta(days=1)
            for fb_dt, fb_v in fb_by_date:
                if abs((fb_dt - tk_dt).total_seconds()) <= window.total_seconds():
                    if fb_v["id"] not in matched_ids:
                        fb_match = fb_v
                        match_reason = f"date (~{abs((fb_dt-tk_dt).days)}d apart)"
                        break

        if fb_match and fb_match["id"] not in matched_ids:
            matched += 1
            matched_ids.add(fb_match["id"])
            label = (tk.get("title") or tk["id"])[:50]
            match_log.append(f"  [{match_reason}] {label}")

            if not dry_run:
                with conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO posted_videos
                            (channel_id, tiktok_video_id, tiktok_url, tiktok_title,
                             tiktok_timestamp, fb_video_id, posted_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded')
                    """, (
                        channel_id, tk["id"], tk.get("url"), tk.get("title"),
                        tk.get("timestamp"), fb_match["id"],
                        fb_match.get("created_time", ""),
                    ))
                    conn.execute("""
                        UPDATE posted_videos
                        SET status='uploaded', fb_video_id=?, posted_at=?
                        WHERE channel_id=? AND tiktok_video_id=? AND status!='uploaded'
                    """, (
                        fb_match["id"], fb_match.get("created_time", ""),
                        channel_id, tk["id"],
                    ))

    conn.close()

    print(f"Matched {matched} / {len(tiktok_videos)} TikTok videos to existing FB posts:\n")
    for line in match_log:
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode('ascii', errors='replace').decode())

    if dry_run:
        print("\nDRY RUN — no changes written to DB.")
    else:
        print(f"\nDone. {matched} TikTok videos marked as already-posted.")
        print("These will never be reposted by the bot.")


if __name__ == "__main__":
    main()
