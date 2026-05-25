"""
Runs the full TikTok→Facebook pipeline for a single channel, one slot.
Called by orchestrator.py — never runs all channels directly.
Returns a result dict so orchestrator can aggregate and notify.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

from . import db
from .config import load_credentials, load_token
from .tiktok_downloader import (
    get_profile_videos, download_video, is_short_video,
    cleanup_download, cleanup_stale_downloads,
)
from .facebook_poster import (
    upload_video, check_token_expiry, TOKEN_EXPIRY_WARNING_DAYS,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"


def run_channel(channel: Dict[str, Any], slot: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    Full pipeline for one channel, one slot.
    Returns: {channel_id, slot, status, video_uploaded, fb_url, error, token_warning}
    Never raises.
    """
    channel_id = channel["id"]
    result = {
        "channel_id": channel_id,
        "slot": slot,
        "status": "skipped",
        "video_uploaded": None,
        "fb_url": None,
        "error": None,
        "token_warning": None,
    }

    run_id = db.start_run(channel_id, slot)

    try:
        if db.slot_already_ran(channel_id, slot):
            logger.info("[%s] Slot %d already ran today — skipping", channel_id, slot)
            db.finish_run(run_id, "skipped")
            return result

        db.upsert_channel(channel)
        cleanup_stale_downloads(DOWNLOADS_DIR, max_age_days=7)

        # Load credentials and token
        try:
            creds = load_credentials(channel["credentials_file"])
            token_data = load_token(channel["token_file"])
        except FileNotFoundError as exc:
            error_msg = str(exc)
            logger.error("[%s] %s", channel_id, error_msg)
            db.finish_run(run_id, "failed", error_message=error_msg)
            result["status"] = "failed"
            result["error"] = error_msg
            return result

        # Token expiry checks
        is_expired, days_left = check_token_expiry(token_data)
        if is_expired:
            error_msg = f"Page access token is expired. Run: python refresh_token.py --page {channel_id}"
            logger.error("[%s] %s", channel_id, error_msg)
            db.finish_run(run_id, "failed", error_message=error_msg)
            result["status"] = "failed"
            result["error"] = error_msg
            return result

        if days_left <= TOKEN_EXPIRY_WARNING_DAYS:
            warn = f"Token expires in {days_left} days. Run: python refresh_token.py --page {channel_id}"
            logger.warning("[%s] %s", channel_id, warn)
            result["token_warning"] = warn

        page_id = creds["page_id"]
        page_token = token_data["page_access_token"]

        # Pick one video to upload this slot
        video = _pick_next_video(channel)
        if video is None:
            logger.info("[%s] No unposted videos available for slot %d", channel_id, slot)
            db.finish_run(run_id, "no_content")
            result["status"] = "no_content"
            return result

        logger.info("[%s] Selected video: %s | '%s'", channel_id, video["id"], video.get("title", ""))

        # Download
        local_file = _download_with_retry(channel, video, dry_run)
        if local_file is None:
            _handle_failure(channel, video, "Download failed after retries")
            db.finish_run(run_id, "failed", error_message="Download failed")
            result["status"] = "failed"
            result["error"] = "Download failed"
            return result

        is_reel = is_short_video(
            duration=video.get("duration"),
            width=video.get("width"),
            height=video.get("height"),
            max_seconds=channel.get("shorts_max_seconds", 180),
        )

        title = video.get("title") or video["id"]
        description = _build_description(video, channel)

        fb_video_id = upload_video(
            page_id=page_id,
            page_access_token=page_token,
            video_path=local_file,
            title=title,
            description=description,
            is_reel=is_reel,
            dry_run=dry_run,
        )

        if fb_video_id:
            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], fb_video_id)
                db.finish_run(run_id, "success", videos_uploaded=1)
            else:
                # Dry run: do NOT write to DB so real runs aren't blocked
                db.finish_run(run_id, "dry_run", videos_uploaded=0)
                logger.info("[%s] [DRY RUN] Would have uploaded: https://www.facebook.com/video/%s", channel_id, fb_video_id)
            cleanup_download(local_file)
            result["status"] = "success"
            result["video_uploaded"] = title
            result["fb_url"] = f"https://www.facebook.com/video/{fb_video_id}"
            if not dry_run:
                logger.info("[%s] ✓ Uploaded: %s", channel_id, result["fb_url"])
        else:
            _handle_failure(channel, video, "Upload returned no video ID")
            db.finish_run(run_id, "failed", error_message="Upload failed")
            result["status"] = "failed"
            result["error"] = "Upload returned no video ID"

    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.exception("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    return result


# ── Video selection ───────────────────────────────────────────────────────────

def _pick_next_video(channel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    channel_id = channel["id"]
    today = date.today()

    retries = db.get_videos_for_retry(channel_id, today)
    if retries:
        logger.info("[%s] Found %d video(s) due for retry", channel_id, len(retries))
        return {
            "id": retries[0]["tiktok_video_id"],
            "url": retries[0]["tiktok_url"],
            "title": retries[0]["tiktok_title"],
            "timestamp": retries[0]["tiktok_timestamp"],
        }

    videos = get_profile_videos(channel["tiktok_username"])
    if not videos:
        return None

    already_posted = db.get_posted_video_ids(channel_id)

    for video in videos:
        if video["id"] not in already_posted:
            db.record_video_seen(channel_id, video)
            return video

    return None


# ── Download ──────────────────────────────────────────────────────────────────

def _download_with_retry(channel: Dict[str, Any], video: Dict[str, Any],
                         dry_run: bool) -> Optional[Path]:
    if dry_run:
        logger.info("[DRY RUN] Skipping download for %s", video["id"])
        return DOWNLOADS_DIR / f"{video['id']}.mp4"

    channel_dir = DOWNLOADS_DIR / channel["id"]
    return download_video(
        video_url=video["url"],
        video_id=video["id"],
        output_dir=channel_dir,
    )


def _handle_failure(channel: Dict[str, Any], video: Dict[str, Any], error_msg: str) -> None:
    today = date.today()
    db.mark_retry(
        channel_id=channel["id"],
        tiktok_video_id=video["id"],
        error_message=error_msg,
        next_retry_date=today + timedelta(days=1),
        max_retries=channel.get("max_retry_days", 3),
    )
    logger.warning("[%s] Video %s queued for retry tomorrow: %s",
                   channel["id"], video["id"], error_msg)


# ── Description ───────────────────────────────────────────────────────────────

def _build_description(video: Dict[str, Any], channel: Dict[str, Any]) -> str:
    parts = [video.get("description") or ""]
    footer = channel.get("description_footer", "")
    if footer:
        parts.append(footer)
    return "\n\n".join(p for p in parts if p).strip()
