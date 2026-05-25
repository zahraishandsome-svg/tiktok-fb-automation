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
) -> Optional[str]:
    """
    Upload a video to a Facebook Page.
    Returns the Facebook video ID on success, None on failure.
    For short/vertical videos (is_reel=True), uses the Reels endpoint for better reach.
    """
    if dry_run:
        logger.info("[DRY RUN] Would upload '%s' to page %s (reel=%s)", title, page_id, is_reel)
        return "dry_run_video_id"

    file_size = video_path.stat().st_size
    logger.info("Uploading to Facebook page %s | size=%.1f MB | reel=%s",
                page_id, file_size / 1_048_576, is_reel)

    if is_reel:
        return _upload_reel(page_id, page_access_token, video_path, title, description)
    elif file_size < 1_048_576:
        return _upload_simple(page_id, page_access_token, video_path, title, description)
    else:
        return _upload_resumable(page_id, page_access_token, video_path, title, description, file_size)


def _upload_simple(page_id: str, token: str, video_path: Path,
                   title: str, description: str) -> Optional[str]:
    """Simple (non-resumable) upload for small videos < 1 MB."""
    url = f"{GRAPH_VIDEO_URL}/{page_id}/videos"
    try:
        with open(video_path, "rb") as f:
            resp = _post_with_retry(
                url,
                data={"title": title, "description": description, "access_token": token},
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
                      title: str, description: str, file_size: int) -> Optional[str]:
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
        upload_session_id = init_data.get("upload_session_id")
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
        resp = _post_with_retry(
            base_url,
            data={
                "upload_phase": "finish",
                "upload_session_id": upload_session_id,
                "title": title,
                "description": description,
                "access_token": token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        finish_data = resp.json()
        video_id = finish_data.get("video_id") or finish_data.get("id")
        if video_id:
            logger.info("Resumable upload complete. FB video ID: %s", video_id)
            _wait_for_processing(video_id, token)
        return video_id
    except requests.RequestException as exc:
        logger.error("Resumable upload finish failed: %s", exc)
        return None


def _upload_reel(page_id: str, token: str, video_path: Path,
                 title: str, description: str) -> Optional[str]:
    """Upload as a Facebook Reel for better reach on short vertical videos."""
    url = f"{GRAPH_VIDEO_URL}/{page_id}/video_reels"
    file_size = video_path.stat().st_size

    # Initialize reel upload
    try:
        resp = _post_with_retry(
            url,
            data={
                "upload_phase": "start",
                "file_size": file_size,
                "access_token": token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        init_data = resp.json()
        upload_session_id = init_data.get("upload_session_id")
        video_id = init_data.get("video_id")
        start_offset = int(init_data.get("start_offset", 0))
        end_offset = int(init_data.get("end_offset", file_size))
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.error("Reel upload init failed: %s", exc)
        return None

    # Upload the video binary (single request for reels)
    with open(video_path, "rb") as f:
        f.seek(start_offset)
        video_data = f.read(end_offset - start_offset)

    try:
        _post_with_retry(
            url,
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
        logger.error("Reel upload transfer failed: %s", exc)
        return None

    # Finish and publish
    try:
        resp = _post_with_retry(
            url,
            data={
                "upload_phase": "finish",
                "upload_session_id": upload_session_id,
                "video_id": video_id,
                "title": title,
                "description": description,
                "access_token": token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        logger.info("Reel upload complete. FB video ID: %s", video_id)
        _wait_for_processing(video_id, token)
        return video_id
    except requests.RequestException as exc:
        logger.error("Reel upload finish failed: %s", exc)
        return None


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
