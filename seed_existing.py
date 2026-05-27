#!/usr/bin/env python3
"""
Seed the DB with videos already posted to Facebook so they're never reposted.

Fetches existing videos from the Facebook Page and the TikTok profile,
matches by normalized title, and marks matched TikTok videos as 'uploaded'.

Usage:
  python seed_existing.py --page page_1
  python seed_existing.py --page page_1 --dry-run   # show matches without writing
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
from src.config import load_config, load_credentials, load_token
from src.db import init_db, get_connection
from src.tiktok_downloader import get_profile_videos
from src.facebook_poster import GRAPH_URL
from datetime import datetime


def normalize(text: str) -> str:
    """Lowercase, strip emoji, collapse whitespace for fuzzy title matching."""
    text = text.lower()
    # Remove emoji (basic unicode ranges)
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    text = re.sub(r'[\U0001F600-\U0001F64F]', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_fb_page_videos(page_id: str, token: str) -> list:
    """Fetch existing videos from Facebook Page."""
    videos = []
    url = f"{GRAPH_URL}/{page_id}/videos"
    params = {
        "fields": "title,description,created_time",
        "access_token": token,
        "limit": 100,
    }
    while url:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 500:
            print(f"  FB API 500 on pagination — using {len(videos)} videos fetched so far")
            break
        resp.raise_for_status()
        data = resp.json()
        videos.extend(data.get("data", []))
        next_page = data.get("paging", {}).get("next")
        url = next_page
        params = {}  # next URL already has all params
    return videos


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed already-posted videos into DB")
    parser.add_argument("--page", required=True, metavar="CHANNEL_ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show matches without writing to DB")
    args = parser.parse_args()

    config = load_config()
    channels = {ch["id"]: ch for ch in config["channels"]}

    if args.page not in channels:
        print(f"ERROR: Channel '{args.page}' not found in channels.yaml")
        sys.exit(1)

    channel = channels[args.page]
    init_db()

    creds = load_credentials(channel["credentials_file"])
    token_data = load_token(channel["token_file"])
    page_id = creds["page_id"]
    page_token = token_data["page_access_token"]

    print(f"Fetching existing videos from Facebook Page {page_id}...")
    fb_videos = get_fb_page_videos(page_id, page_token)
    print(f"  Found {len(fb_videos)} videos on Facebook")

    fb_titles = {normalize(v.get("title") or ""): v for v in fb_videos if v.get("title")}

    print(f"Fetching TikTok profile: @{channel['tiktok_username']}...")
    tiktok_videos = get_profile_videos(channel["tiktok_username"])
    if tiktok_videos is None:
        print(f"ERROR: Could not fetch TikTok profile @{channel['tiktok_username']} — network error or profile unreachable")
        sys.exit(1)
    print(f"  Found {len(tiktok_videos)} videos on TikTok")

    matched = 0
    conn = get_connection()

    for video in tiktok_videos:
        norm_title = normalize(video.get("title") or "")
        if norm_title in fb_titles:
            fb_video = fb_titles[norm_title]
            print(f"  MATCH: '{video['title']}' -> FB video {fb_video.get('id')}")
            matched += 1

            if not args.dry_run:
                with conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO posted_videos
                            (channel_id, tiktok_video_id, tiktok_url, tiktok_title,
                             tiktok_timestamp, fb_video_id, posted_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded')
                    """, (
                        channel["id"],
                        video["id"],
                        video.get("url"),
                        video.get("title"),
                        video.get("timestamp"),
                        fb_video.get("id"),
                        fb_video.get("created_time", datetime.utcnow().isoformat()),
                    ))
                    conn.execute("""
                        UPDATE posted_videos SET status = 'uploaded', fb_video_id = ?, posted_at = ?
                        WHERE channel_id = ? AND tiktok_video_id = ? AND status != 'uploaded'
                    """, (
                        fb_video.get("id"),
                        fb_video.get("created_time", datetime.utcnow().isoformat()),
                        channel["id"],
                        video["id"],
                    ))

    conn.close()

    print(f"\nMatched {matched} / {len(tiktok_videos)} TikTok videos to existing FB posts.")
    if args.dry_run:
        print("DRY RUN — no changes written to DB.")
    else:
        print("Seeding complete. These videos will not be reposted.")


if __name__ == "__main__":
    main()
