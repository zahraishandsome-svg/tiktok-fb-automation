#!/usr/bin/env python3
"""
Refresh Facebook Page tokens for ALL pages in channels.yaml at once.

Usage:
  python refresh_all_tokens.py --token "EAAxxxxx..."

Steps:
  1. Takes a short-lived user token from Graph API Explorer
  2. Exchanges it for a long-lived user token (~60 days)
  3. Calls /me/accounts to get tokens for ALL managed pages in one shot
  4. Matches pages to channels by page_id from credentials files
  5. Saves all token files at once — no per-page steps needed

Get token at: https://developers.facebook.com/tools/explorer/
Required permissions: pages_manage_posts, pages_show_list, publish_video
"""

import argparse
import sys
import requests
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config, load_credentials, save_token
from src.facebook_poster import get_long_lived_token

GRAPH_URL = "https://graph.facebook.com/v19.0"


def get_all_page_tokens(user_token: str) -> dict:
    """Call /me/accounts to get tokens for all pages managed by this user."""
    all_pages = {}
    url = f"{GRAPH_URL}/me/accounts"
    params = {"access_token": user_token, "limit": 100}

    while url:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("data", []):
            all_pages[page["id"]] = page["access_token"]
        next_url = data.get("paging", {}).get("next")
        url = next_url if next_url else None
        params = {}

    return all_pages


def main():
    parser = argparse.ArgumentParser(description="Refresh ALL Facebook page tokens at once")
    parser.add_argument("--token", required=True,
                        help="Short-lived user token from developers.facebook.com/tools/explorer/")
    args = parser.parse_args()

    config = load_config()
    channels = config["channels"]

    if not channels:
        print("No channels found in channels.yaml")
        sys.exit(1)

    # Use first channel's app credentials for the token exchange
    first_creds = load_credentials(channels[0]["credentials_file"])
    app_id = first_creds["app_id"]
    app_secret = first_creds["app_secret"]

    print(f"Exchanging short-lived token for long-lived user token (app: {app_id})...")
    token_resp = get_long_lived_token(args.token, app_id, app_secret)
    long_lived_token = token_resp["access_token"]
    expires_at = (date.today() + timedelta(days=58)).isoformat()
    print(f"Got long-lived token. Expires ~{expires_at}\n")

    print("Fetching all page tokens via /me/accounts...")
    all_page_tokens = get_all_page_tokens(long_lived_token)
    print(f"Found {len(all_page_tokens)} page(s) under this account\n")

    updated = 0
    skipped = 0
    for channel in channels:
        if not channel.get("enabled", True):
            continue
        creds = load_credentials(channel["credentials_file"])
        page_id = creds["page_id"]

        if page_id not in all_page_tokens:
            print(f"⚠️  {channel['id']} ({channel['facebook_page_name']}) — page {page_id} not found. "
                  f"Make sure this page is connected to the app in Graph Explorer.")
            skipped += 1
            continue

        token_data = {
            "page_access_token": all_page_tokens[page_id],
            "user_access_token": long_lived_token,
            "expires_at": expires_at,
        }
        save_token(channel["token_file"], token_data)
        print(f"✓  {channel['id']} ({channel['facebook_page_name']}) → saved to {channel['token_file']}")
        updated += 1

    print(f"\n{'='*50}")
    print(f"Done. {updated} refreshed, {skipped} skipped.")
    if updated > 0:
        print(f"Test with: python run.py --slot 1 --dry-run")


if __name__ == "__main__":
    main()
