"""
Discord webhook notifications.
Only fires when DISCORD_WEBHOOK_URL is set — silent otherwise.
"""

import logging
import requests
from datetime import date
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def send_failure_alert(webhook_url: str, failures: List[Dict[str, Any]], slot: int) -> None:
    if not webhook_url:
        return
    lines = [f"🚨 **TikTok→FB Automation | Slot {slot} Failures** ({date.today()})"]
    for f in failures:
        lines.append(f"• `{f['channel_id']}` — {f.get('error', 'unknown error')}")
    _post(webhook_url, "\n".join(lines))


def send_token_warning(webhook_url: str, warnings: List[Dict[str, Any]]) -> None:
    if not webhook_url or not warnings:
        return
    lines = [f"⚠️ **Token Expiring Soon** ({date.today()})"]
    for w in warnings:
        lines.append(f"• `{w['channel_id']}` ({w.get('page_name', '')}) — {w['warning']}")
    _post(webhook_url, "\n".join(lines))


def send_daily_summary(
    webhook_url: str,
    db_rows: List[Dict[str, Any]],
    channel_names: Dict[str, str] = None,
) -> None:
    if not webhook_url:
        return

    channel_names = channel_names or {}

    def label(channel_id: str) -> str:
        name = channel_names.get(channel_id)
        return f"`{channel_id}` ({name})" if name else f"`{channel_id}`"

    success_rows = [r for r in db_rows if r["status"] == "success"]
    failed_rows = [r for r in db_rows if r["status"] == "failed"]
    no_content_rows = [r for r in db_rows if r["status"] == "no_content"]

    emoji = "✅" if not failed_rows else "⚠️"
    lines = [
        f"{emoji} **TikTok→Facebook Daily Summary** ({date.today()})",
        f"Uploaded: {len(success_rows)} | Failed: {len(failed_rows)} | No content: {len(no_content_rows)}",
    ]

    for r in success_rows:
        fb_id = r.get("fb_video_id")
        url = f"https://www.facebook.com/video/{fb_id}" if fb_id else "?"
        lines.append(f"  ✓ {label(r['channel_id'])} slot {r.get('slot', '?')} → {url}")
    for r in failed_rows:
        lines.append(f"  ✗ {label(r['channel_id'])} slot {r.get('slot', '?')} — {r.get('error_message', '?')}")
    for r in no_content_rows:
        lines.append(f"  — {label(r['channel_id'])} slot {r.get('slot', '?')}: no new content")

    _post(webhook_url, "\n".join(lines))


def _post(webhook_url: str, content: str) -> None:
    try:
        resp = requests.post(
            webhook_url,
            json={"content": content[:2000]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        logger.debug("Discord notification sent")
    except requests.RequestException as exc:
        logger.warning("Discord notification failed: %s", exc)
