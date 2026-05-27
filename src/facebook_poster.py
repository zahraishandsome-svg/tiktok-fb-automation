"""
Facebook Graph API video upload.
Handles token management, resumable uploads, Reels vs regular video, and processing polling.
"""

import json
import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import requests

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v19.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
GRAPH_VIDEO_URL = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}"
CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB chunks for resumable upload
TOKEN_EXPIRY_WARNING_DAYS = 7
PROCESSING_POLL_INTERVAL = 10   # seconds between status checks
PROCESSING_TIMEOUT = 300        # seconds before giving up on processing check

_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
_MAX_UPLOAD_RETRIES = 5


def _post_with_retry(url: str, *, data: dict, files: dict = None,
                     timeout: int = 300) -> requests.Response:
    """
    POST with exponential backoff on transient 5xx errors.
    Raises requests.RequestException on permanent failure.
    """
    for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
        resp = requests.post(url, data=data, files=files, timeout=timeout)
        if resp.status_code not in _TRANSIENT_STATUS_CODES:
            resp.raise_for_status()
            return resp
        if attempt < _MAX_UPLOAD_RETRIES:
            wait = min(60, (2 ** attempt) + random.uniform(0, 1))
            logger.warning(
                "Facebook API transient %d — retry %d/%d in %.1fs",
                resp.status_code, attempt, _MAX_UPLOAD_RETRIES, wait,
            )
            time.sleep(wait)
        else:
            resp.raise_for_status()   # raises HTTPError
    return resp  # unreachable, satisfies type checker


# ── Token management ──────────────────────────────────────────────────────────

def get_long_lived_token(short_lived_token: str, app_id: str, app_secret: str) -> Dict[str, Any]:
    """Exchange a short-lived user token for a long-lived one (~60 days)."""
    resp = requests.get(
        f"{GRAPH_URL}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"Token exchange failed: {data}")
    return data


def get_page_access_token(user_long_lived_token: str, page_id: str) -> str:
    """Get a Page access token from a long-lived user token."""
    resp = requests.get(
        f"{GRAPH_URL}/{page_id}",
        params={
            "fields": "access_token",
            "access_token": user_long_lived_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"Could not get page access token: {data}")
    return data["access_token"]


def check_token_expiry(token_data: Dict[str, Any]) -> Tuple[bool, int]:
    """
    Returns (is_expired, days_until_expiry).
    is_expired=True means token is already expired.
    """
    expires_at_str = token_data.get("expires_at")
    if not expires_at_str:
        # No expiry stored — assume it's fine but warn
        return False, 999

    try:
        expires_at = date.fromisoformat(expires_at_str)
    except ValueError:
        logger.warning("Could not parse expires_at: %s", expires_at_str)
        return False, 999

    today = date.today()
    days_left = (expires_at - today).days

    if days_left < 0:
        return True, days_left
    return False, days_left


# ── Video upload ──────────────────────────────────────────────────────────────

def upload_video(
    page_id: str,
    page_access_token: str,
    video_path: Path,
    title: str,
    description: str,
    is_reel: bool = False,
    dry_run: bool = False,
    scheduled_publish_time: Optional[int] = None,
) -> Optional[str]:
    """
    Upload a video to a Facebook Page.
    Returns the Facebook video ID on success, None on failure.
    If scheduled_publish_time (UTC Unix timestamp) is provided, the video is
    uploaded as a draft and published at that time.
    For short/vertical videos (is_reel=True), uses the Reels endpoint for better reach.
    """
    if dry_run:
        sched = f" | scheduled={scheduled_publish_time}" if scheduled_publish_time else ""
        logger.info("[DRY RUN] Would upload '%s' to page %s (reel=%s%s)", title, page_id, is_reel, sched)
        return "dry_run_video_id"

    file_size = video_path.stat().st_size
    logger.info("Uploading to Facebook page %s | size=%.1f MB | reel=%s | scheduled=%s",
                page_id, file_size / 1_048_576, is_reel, scheduled_publish_time)

    if is_reel:
        return _upload_reel(page_id, page_access_token, video_path, title, description, scheduled_publish_time)
    elif file_size < 1_048_576:
        return _upload_simple(page_id, page_access_token, video_path, title, description, scheduled_publish_time)
    else:
        return _upload_resumable(page_id, page_access_token, video_path, title, description, file_size, scheduled_publish_time)


def _upload_simple(page_id: str, token: str, video_path: Path,
                   title: str, description: str,
                   scheduled_publish_time: Optional[int] = None) -> Optional[str]:
    """Simple (non-resumable) upload for small videos < 1 MB."""
    url = f"{GRAPH_VIDEO_URL}/{page_id}/videos"
    try:
        post_data = {"title": title, "description": description, "access_token": token}
        if scheduled_publish_time:
            post_data["scheduled_publish_time"] = scheduled_publish_time
            post_data["published"] = "false"
        with open(video_path, "rb") as f:
            resp = _post_with_retry(
                url,
                data=post_data,
                files={"source": f},
                timeout=300,
            )
        data = resp.json()
        video_id = data.get("id")
        if video_id:
            logger.info("Simple upload complete. FB video ID: %s", video_id)
            _wait_for_processing(video_id, token)
        return video_id
    except requests.RequestException as exc:
        logger.error("Simple upload failed: %s", exc)
        return None


def _upload_resumable(page_id: str, token: str, video_path: Path,
                      title: str, description: str, file_size: int,
                      scheduled_publish_time: Optional[int] = None) -> Optional[str]:
    """Resumable (chunked) upload for videos >= 1 MB."""
    base_url = f"{GRAPH_VIDEO_URL}/{page_id}/videos"

    # Step 1: Initialize
    try:
        resp = _post_with_retry(
            base_url,
            data={
                "upload_phase": "start",
                "file_size": file_size,
                "access_token": token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        init_data = resp.json()
        logger.debug("Resumable upload init response: %s", init_data)
        upload_session_id = init_data.get("upload_session_id")
        video_id = init_data.get("video_id")   # FB returns video_id at start phase
        start_offset = int(init_data.get("start_offset", 0))
        end_offset = int(init_data.get("end_offset", CHUNK_SIZE))
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.error("Resumable upload init failed: %s", exc)
        return None

    logger.debug("Upload session %s started", upload_session_id)

    # Step 2: Transfer chunks
    with open(video_path, "rb") as f:
        while True:
            f.seek(start_offset)
            chunk = f.read(end_offset - start_offset)
            if not chunk:
                break

            try:
                resp = _post_with_retry(
                    base_url,
                    data={
                        "upload_phase": "transfer",
                        "start_offset": start_offset,
                        "upload_session_id": upload_session_id,
                        "access_token": token,
                    },
                    files={"video_file_chunk": chunk},
                    timeout=300,
                )
                resp.raise_for_status()
                transfer_data = resp.json()
                new_start = int(transfer_data.get("start_offset", end_offset))
                new_end = int(transfer_data.get("end_offset", new_start + CHUNK_SIZE))
            except (requests.RequestException, ValueError, KeyError) as exc:
                logger.error("Chunk upload failed at offset %d: %s", start_offset, exc)
                return None

            logger.debug("Uploaded chunk: %d–%d / %d", start_offset, end_offset, file_size)

            if new_start >= file_size:
                break
            start_offset = new_start
            end_offset = min(new_end, file_size)

    # Step 3: Finish
    try:
        finish_payload = {
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": title,
            "description": description,
            "access_token": token,
        }
        if scheduled_publish_time:
            finish_payload["scheduled_publish_time"] = scheduled_publish_time
            finish_payload["published"] = "false"
        resp = _post_with_retry(base_url, data=finish_payload, timeout=60)
        resp.raise_for_status()
        finish_data = resp.json()
        logger.debug("Resumable upload finish response: %s", finish_data)
        # video_id comes from the init phase; finish returns {"success": true}
        if not video_id:
            video_id = finish_data.get("video_id") or finish_data.get("id")
        if video_id:
            logger.info("Resumable upload complete. FB video ID: %s", video_id)
            _wait_for_processing(video_id, token)
        else:
            logger.error("Resumable upload: no video_id in init or finish response. finish=%s", finish_data)
        return video_id
    except requests.RequestException as exc:
        logger.error("Resumable upload finish failed: %s", exc)
        return None


def _upload_reel(page_id: str, token: str, video_path: Path,
                 title: str, description: str,
                 scheduled_publish_time: Optional[int] = None) -> Optional[str]:
    """
    Upload as a Facebook Reel using the current Graph API flow:
      1. Start  → POST /page_id/video_reels?upload_phase=start  → {video_id, upload_url}
      2. Upload → POST upload_url (rupload.facebook.com) with OAuth header + offset headers
      3. Finish → POST /page_id/video_reels?upload_phase=finish with video_id + metadata
    """
    endpoint = f"{GRAPH_VIDEO_URL}/{page_id}/video_reels"
    file_size = video_path.stat().st_size

    # ── 1. Start ──────────────────────────────────────────────────────────────
    try:
        resp = _post_with_retry(
            endpoint,
            data={"upload_phase": "start", "file_size": file_size, "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        init_data = resp.json()
        video_id = init_data.get("video_id")
        upload_url = init_data.get("upload_url")
        if not video_id or not upload_url:
            # Fallback: old-style transfer phase (some API versions don't return upload_url)
            logger.warning("Reel init missing upload_url — falling back to transfer phase. init=%s", init_data)
            upload_url = None
            upload_session_id = init_data.get("upload_session_id")
            start_offset = int(init_data.get("start_offset", 0))
            end_offset = int(init_data.get("end_offset", file_size))
        else:
            upload_session_id = None
            logger.debug("Reel init OK. video_id=%s upload_url=%s…", video_id, upload_url[:60])
    except (requests.RequestException, ValueError) as exc:
        logger.error("Reel upload init failed: %s", exc)
        return None

    # ── 2. Upload ─────────────────────────────────────────────────────────────
    if upload_url:
        # New rupload.facebook.com approach
        try:
            with open(video_path, "rb") as f:
                video_data = f.read()
            upload_resp = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(file_size),
                },
                data=video_data,
                timeout=300,
            )
            upload_resp.raise_for_status()
            logger.debug("Reel upload OK: %s", upload_resp.text[:100])
        except requests.RequestException as exc:
            logger.error("Reel upload transfer failed: %s", exc)
            return None
    else:
        # Old transfer-phase fallback
        try:
            with open(video_path, "rb") as f:
                f.seek(start_offset)
                video_data = f.read(end_offset - start_offset)
            _post_with_retry(
                endpoint,
                data={
                    "upload_phase": "transfer",
                    "start_offset": start_offset,
                    "upload_session_id": upload_session_id,
                    "access_token": token,
                },
                files={"video_file_chunk": video_data},
                timeout=300,
            )
        except requests.RequestException as exc:
            logger.error("Reel upload transfer (legacy) failed: %s", exc)
            return None

    # ── 3. Finish ─────────────────────────────────────────────────────────────
    try:
        finish_payload = {
            "upload_phase": "finish",
            "video_id": video_id,
            "title": title,
            "description": description,
            "access_token": token,
        }
        if scheduled_publish_time:
            # Reels API: use video_state=SCHEDULED, not published=false
            finish_payload["video_state"] = "SCHEDULED"
            finish_payload["scheduled_publish_time"] = scheduled_publish_time
        else:
            finish_payload["video_state"] = "PUBLISHED"

        # Use requests.post directly so we can log FB's actual error body on 400
        resp = requests.post(endpoint, data=finish_payload, timeout=60)
        if not resp.ok:
            logger.error(
                "Reel upload finish failed: HTTP %s | payload=%s | response=%s",
                resp.status_code,
                {k: v for k, v in finish_payload.items() if k != "access_token"},
                resp.text,
            )
            return None
        logger.info("Reel finish phase complete. FB video ID: %s", video_id)
    except requests.RequestException as exc:
        logger.error("Reel upload finish failed (network): %s", exc)
        return None

    # ── 4. Explicit publish for immediate posts ────────────────────────────────
    # Reels API ignores video_state=PUBLISHED in finish phase — explicit call needed
    if not scheduled_publish_time:
        try:
            pub_resp = requests.post(
                f"{GRAPH_URL}/{video_id}",
                data={"published": "true", "access_token": token},
                timeout=60,
            )
            pub_resp.raise_for_status()
            logger.info("Reel published. FB video ID: %s", video_id)
        except requests.RequestException as exc:
            logger.warning(
                "Reel publish call failed for %s (video may still be a draft): %s",
                video_id, exc,
            )

    return video_id


def _wait_for_processing(video_id: str, token: str) -> None:
    """
    Poll until Facebook finishes processing the video.
    Times out after PROCESSING_TIMEOUT seconds and logs a warning (does not fail).
    """
    deadline = time.time() + PROCESSING_TIMEOUT
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{GRAPH_URL}/{video_id}",
                params={"fields": "status", "access_token": token},
                timeout=30,
            )
            resp.raise_for_status()
            status = resp.json().get("status", {})
            progress = status.get("processing_progress", 0)
            if progress >= 100:
                logger.info("Video %s processing complete (100%%)", video_id)
                return
            logger.debug("Video %s processing: %s%%", video_id, progress)
        except requests.RequestException as exc:
            logger.warning("Could not check processing status for %s: %s", video_id, exc)

        time.sleep(PROCESSING_POLL_INTERVAL)

    logger.warning("Video %s processing did not complete within %ds — continuing anyway",
                   video_id, PROCESSING_TIMEOUT)
