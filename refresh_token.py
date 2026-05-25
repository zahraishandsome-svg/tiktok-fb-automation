#!/usr/bin/env python3
"""
Refresh a Facebook Page access token.

Usage:
  python refresh_token.py --page page_1 --token "EAAxxxxx..."

Steps:
  1. Takes a short-lived user token from Graph API Explorer
  2. Exchanges it for a long-lived user token (~60 days)
  3. Fetches a Page access token from that
  4. Saves to tokens/page_N_token.json with expiry date
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config, load_credentials, save_token
from src.facebook_poster import get_long_lived_token, get_page_access_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Facebook Page access token")
    parser.add_argument("--page", required=True, metavar="CHANNEL_ID",
                        help="Channel ID from channels.yaml (e.g. page_1)")
    parser.add_argument("--token", required=True, metavar="SHORT_LIVED_TOKEN",
                        help="Short-lived user token from developers.facebook.com/tools/explorer/")
    args = parser.parse_args()

    config = load_config()
    channels = {ch["id"]: ch for ch in config["channels"]}

    if args.page not in channels:
        print(f"ERROR: Channel '{args.page}' not found in channels.yaml")
        sys.exit(1)

    channel = channels[args.page]

    print(f"Loading credentials from: {channel['credentials_file']}")
    creds = load_credentials(channel["credentials_file"])
    app_id = creds["app_id"]
    app_secret = creds["app_secret"]
    page_id = creds["page_id"]

    print("Exchanging short-lived token for long-lived user token...")
    token_resp = get_long_lived_token(
        short_lived_token=args.token,
        app_id=app_id,
        app_secret=app_secret,
    )
    user_long_lived_token = token_resp["access_token"]
    # Facebook tokens last ~60 days; store expiry conservatively as 58 days from now
    expires_at = (date.today() + timedelta(days=58)).isoformat()
    print(f"Got long-lived user token. Expires ~{expires_at}")

    print(f"Fetching Page access token for page_id={page_id}...")
    page_token = get_page_access_token(user_long_lived_token, page_id)
    print("Got Page access token.")

    token_data = {
        "page_access_token": page_token,
        "user_access_token": user_long_lived_token,
        "expires_at": expires_at,
    }

    save_token(channel["token_file"], token_data)
    print(f"Saved to: {channel['token_file']}")
    print(f"Token expires: {expires_at}")
    print("Done. Run: python run.py --slot 1 --channel", args.page, "--dry-run")


if __name__ == "__main__":
    main()
