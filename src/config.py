"""
Load and validate channels.yaml + .env settings.
"""

import json
import os
import yaml
import logging
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


def load_config() -> Dict[str, Any]:
    """Load full config: channels.yaml + .env merged into one dict."""
    load_dotenv(PROJECT_ROOT / ".env")

    channels_file = PROJECT_ROOT / "channels.yaml"
    if not channels_file.exists():
        raise FileNotFoundError(f"channels.yaml not found at {channels_file}")

    with open(channels_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    channels = raw.get("channels", [])
    if not isinstance(channels, list):
        raise ValueError("channels.yaml must have a top-level 'channels' list")

    validated = [_validate_channel(ch) for ch in channels]

    return {
        "channels": validated,
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "project_root": PROJECT_ROOT,
    }


def _validate_channel(ch: Dict[str, Any]) -> Dict[str, Any]:
    required = ["id", "tiktok_username", "facebook_page_name", "credentials_file", "token_file"]
    for field in required:
        if not ch.get(field):
            raise ValueError(f"Channel config missing required field: '{field}'")

    ch["credentials_file"] = PROJECT_ROOT / ch["credentials_file"]
    ch["token_file"] = PROJECT_ROOT / ch["token_file"]

    ch.setdefault("videos_per_day", 2)
    ch.setdefault("description_footer", "")
    ch.setdefault("default_tags", [])
    ch.setdefault("enabled", True)
    ch.setdefault("max_retry_days", 3)
    ch.setdefault("shorts_max_seconds", 180)

    return ch


def load_credentials(credentials_file: Path) -> Dict[str, Any]:
    """Load page credentials JSON: page_id, app_id, app_secret."""
    if not credentials_file.exists():
        raise FileNotFoundError(f"Credentials file not found: {credentials_file}")
    with open(credentials_file, "r") as f:
        return json.load(f)


def load_token(token_file: Path) -> Dict[str, Any]:
    """Load token JSON: page_access_token, expires_at, user_access_token."""
    if not token_file.exists():
        raise FileNotFoundError(f"Token file not found: {token_file}")
    with open(token_file, "r") as f:
        return json.load(f)


def save_token(token_file: Path, token_data: Dict[str, Any]) -> None:
    token_file.parent.mkdir(exist_ok=True)
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)


def get_enabled_channels(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ch for ch in config["channels"] if ch.get("enabled", True)]
