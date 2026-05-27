"""
Runs the full TikTok→Facebook pipeline for a single channel, one slot.
Called by orchestrator.py — never runs all channels directly.
Returns a result dict so orchestrator can aggregate and notify.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from . import db
from .config import load_credentials, load_token
from .tiktok_downloader import (
    get_profile_videos, download_video, is_short_video,
    cleanup_download, cleanup_stale_downloads, _PROFILE_BATCH,
)
from .facebook_poster import (
    upload_video, check_token_expiry, TOKEN_EXPIRY_WARNING_DAYS,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"


class TikTokUnreachableError(Exception):
    """Raised when the TikTok profile fetch fails after all retries."""


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
        video = _pick_next_video(channel, slot)
        if video is None:
            logger.info("[%s] No unposted videos available for slot %d", channel_id, slot)
            db.finish_run(run_id, "no_content")
            result["status"] = "no_content"
            return result

        logger.info("[%s] Selected video: %s | '%s'", channel_id, video["id"], video.get("title", ""))

        # Download
        local_file = _download_with_retry(channel, video, dry_run)
        if local_file is None:
            _handle_download_failure(channel, video, "Download failed after retries")
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

        scheduled_time = _get_scheduled_publish_time(channel, slot, channel_id) if not dry_run else None

        fb_video_id = upload_video(
            page_id=page_id,
            page_access_token=page_token,
            video_path=local_file,
            title=title,
            description=description,
            is_reel=is_reel,
            dry_run=dry_run,
            scheduled_publish_time=scheduled_time,
        )

        if fb_video_id:
            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], fb_video_id)
                db.finish_run(run_id, "success", videos_uploaded=1)
            else:
                db.finish_run(run_id, "dry_run", videos_uploaded=0)
                logger.info("[%s] [DRY RUN] Would have uploaded: https://www.facebook.com/video/%s", channel_id, fb_video_id)
            cleanup_download(local_file)
            result["status"] = "success"
            result["video_uploaded"] = title
            result["fb_url"] = f"https://www.facebook.com/video/{fb_video_id}"
            if not dry_run:
                logger.info("[%s] Uploaded: %s", channel_id, result["fb_url"])
        else:
            _handle_upload_failure(channel, video, "Upload returned no video ID")
            db.finish_run(run_id, "failed", error_message="Upload failed")
            result["status"] = "failed"
            result["error"] = "Upload returned no video ID"

    except TikTokUnreachableError as exc:
        error_msg = str(exc)
        logger.error("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.exception("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    return result


# ── Video selection ───────────────────────────────────────────────────────────

def _pick_next_video(channel: Dict[str, Any], slot: int) -> Optional[Dict[str, Any]]:
    """
    Priority order:
      1. Videos in pending_retry state that are due today (retries take priority)
      2. New unposted videos, sorted newest-first

    Supports two optional channel config keys:
      min_upload_date       YYYY-MM-DD — ignore TikTok videos older than this date.
      min_backlog_for_slot1 int — slot 1 is skipped unless at least this many
                            unuploaded eligible videos exist. When the backlog drops
                            below this threshold the channel automatically falls back
                            to 1 upload/day (slot 2 only).
    """
    channel_id = channel["id"]
    today = date.today()

    # Resolve optional date filter -> Unix timestamp
    min_ts = _parse_min_upload_date(channel.get("min_upload_date"))

    # Check for pending retries first (apply date filter if configured)
    retries = db.get_videos_for_retry(channel_id, today)
    if min_ts is not None:
        retries = [r for r in retries if (r.get("tiktok_timestamp") or 0) >= min_ts]
    if retries:
        logger.info("[%s] Found %d video(s) due for retry", channel_id, len(retries))
        return {
            "id": retries[0]["tiktok_video_id"],
            "url": retries[0]["tiktok_url"],
            "title": retries[0]["tiktok_title"],
            "timestamp": retries[0]["tiktok_timestamp"],
        }

    # Fetch first batch (fast path — newest _PROFILE_BATCH videos only)
    raw_batch = get_profile_videos(channel["tiktok_username"])  # default end=_PROFILE_BATCH
    if raw_batch is None:
        raise TikTokUnreachableError(
            f"TikTok profile @{channel['tiktok_username']} is unreachable after retries"
        )
    if not raw_batch:
        return None

    already_posted = db.get_posted_video_ids(channel_id)

    def _filter(vids):
        if min_ts is not None:
            vids = [v for v in vids if (v.get("timestamp") or 0) >= min_ts]
        return [v for v in vids if v["id"] not in already_posted]

    eligible = _filter(raw_batch)

    # If all videos in the batch are already posted and we got a full batch,
    # there may be older unposted videos — fall back to fetching the full profile.
    if not eligible and len(raw_batch) >= _PROFILE_BATCH:
        logger.info(
            "[%s] All %d videos in first batch already posted — fetching full profile",
            channel_id, _PROFILE_BATCH,
        )
        all_videos = get_profile_videos(channel["tiktok_username"], end=None)
        if all_videos is None:
            raise TikTokUnreachableError(
                f"TikTok profile @{channel['tiktok_username']} is unreachable after retries"
            )
        if min_ts is not None:
            before = len(all_videos)
            all_videos = [v for v in all_videos if (v.get("timestamp") or 0) >= min_ts]
            filtered = before - len(all_videos)
            if filtered:
                logger.info(
                    "[%s] Filtered %d video(s) older than min_upload_date (%s)",
                    channel_id, filtered, channel["min_upload_date"],
                )
        eligible = [v for v in all_videos if v["id"] not in already_posted]

    # Slot throttle: skip slot 1 when backlog is too small so the remaining
    # video(s) are preserved for slot 2.
    min_backlog = channel.get("min_backlog_for_slot1")
    if slot == 1 and min_backlog is not None and len(eligible) < int(min_backlog):
        logger.info(
            "[%s] Slot 1 throttled — %d eligible video(s) available, "
            "need %d (min_backlog_for_slot1). Reserving for slot 2.",
            channel_id, len(eligible), int(min_backlog),
        )
        return None

    for video in eligible:
        db.record_video_seen(channel_id, video)
        return video

    return None


def _parse_min_upload_date(min_date_str: Optional[str]) -> Optional[int]:
    """Convert 'YYYY-MM-DD' string to a Unix timestamp int, or None if not set."""
    if not min_date_str:
        return None
    try:
        return int(datetime.strptime(min_date_str, "%Y-%m-%d").timestamp())
    except ValueError:
        logger.warning(
            "Invalid min_upload_date %r — expected YYYY-MM-DD format, ignoring filter",
            min_date_str,
        )
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


def _handle_download_failure(channel: Dict[str, Any], video: Dict[str, Any],
                              error_msg: str) -> None:
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


def _handle_upload_failure(channel: Dict[str, Any], video: Dict[str, Any],
                            error_msg: str) -> None:
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


# ── Scheduled publish ─────────────────────────────────────────────────────────

def _get_scheduled_publish_time(channel: Dict[str, Any], slot: int,
                                 channel_id: str) -> Optional[int]:
    """
    Calculate the UTC Unix timestamp at which this slot's video should go live.
    Reads slot_publish_times_utc from channel config (e.g. {1: "15:00", 2: "17:00"}).
    Returns a Unix timestamp when the target time is more than 15 minutes in the future.
    Returns None if no publish time is configured or target is too close/past (immediate publish).
    Facebook requires scheduled_publish_time to be at least 10 minutes in the future.
    """
    times = channel.get("slot_publish_times_utc") or {}
    time_str = times.get(slot) or times.get(str(slot))
    if not time_str:
        return None

    try:
        h, m = map(int, str(time_str).split(":"))
    except (ValueError, AttributeError):
        logger.warning("[%s] Invalid slot_publish_times_utc value: %r", channel_id, time_str)
        return None

    now_utc = datetime.now(timezone.utc)
    target = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_seconds = (target - now_utc).total_seconds()

    if delta_seconds < 0:
        # Target already passed today (cron ran late) — publish immediately.
        logger.info(
            "[%s] Slot %d: %02d:%02dZ already passed today (cron delay) — publishing immediately",
            channel_id, slot, h, m,
        )
        return None

    if delta_seconds < 900:
        # Less than 15 min away — too close for FB scheduled publish (minimum 10 min)
        logger.info(
            "[%s] Slot %d target %02d:%02dZ is too close (%.0f s) — publishing immediately",
            channel_id, slot, h, m, delta_seconds,
        )
        return None

    ts = int(target.timestamp())
    logger.info(
        "[%s] Slot %d scheduling publish at %s (%d min from now)",
        channel_id, slot, target.strftime("%Y-%m-%dT%H:%M:%SZ"), int(delta_seconds / 60),
    )
    return ts


# ── Description ───────────────────────────────────────────────────────────────

def _build_description(video: Dict[str, Any], channel: Dict[str, Any]) -> str:
    parts = [video.get("description") or ""]
    footer = channel.get("description_footer", "")
    if footer:
        parts.append(footer)
    return "\n\n".join(p for p in parts if p).strip()
