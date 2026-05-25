#!/usr/bin/env python3
"""
Main entry point for TikTok → Facebook Page automation.

Usage examples:
  python run.py --slot 1                          # First daily upload (all channels)
  python run.py --slot 2                          # Second daily upload (all channels)
  python run.py --slot 1 --channel page_1         # One channel only (testing)
  python run.py --slot 1 --dry-run                # Full pipeline without uploading
  python run.py --slot 1 --dry-run --channel page_1
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config
from src.db import init_db
from src.orchestrator import run_all_channels


def setup_logging(log_level: str) -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(
                open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
            ),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TikTok → Facebook Page Automation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--slot", type=int, choices=[1, 2], required=True,
        help="Upload slot: 1=8 PM PKT (15:00 UTC), 2=1 AM PKT (20:00 UTC)",
    )
    parser.add_argument(
        "--channel", type=str, default=None, metavar="CHANNEL_ID",
        help="Only run this channel ID. Useful for testing a single channel.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run full pipeline but skip the actual Facebook upload.",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    log_file = setup_logging(args.log_level)
    logger = logging.getLogger("run")

    logger.info("=" * 60)
    logger.info("TikTok→Facebook Automation | Slot %d | dry_run=%s", args.slot, args.dry_run)
    logger.info("=" * 60)

    init_db()

    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    effective_dry_run = args.dry_run or config.get("dry_run", False)
    if effective_dry_run and not args.dry_run:
        logger.info("DRY_RUN=true in .env — no uploads will happen")

    results = run_all_channels(
        config=config,
        slot=args.slot,
        channel_filter=args.channel,
        dry_run=effective_dry_run,
    )

    failed = sum(1 for r in results if r["status"] == "failed")
    logger.info("Log saved to: %s", log_file)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
