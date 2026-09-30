"""
run_pipeline.py — Main orchestrator of the OSINT pipeline.

Changes (refactoring):
  - username sanitize via utils.sanitize_username (path-traversal protection)
  - uses utils.db_path_for() for a unified DB file name format
  - checks for DB presence before --only-profiler
  - removed artifact comment "HERE limit=batch_limit WAS ADDED"
  - added cleanup of processed videos at the end.
"""

import argparse
import logging
import sys
import os
import sqlite3

import tiktok_parser_cdp
import vision_manager_3
import audio_manager
import sublimator_cdp
import analyzer_cdp
import profiler_cdp
from utils import sanitize_username, db_path_for, report_path_for

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(module)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("osint_pipeline.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

def cleanup_videos(db_path: str):
    """Deletes local MP4 files that were successfully analyzed (or marked as failed)."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT r.local_path 
            FROM reposts r
            LEFT JOIN analysis a ON r.video_id = a.video_id
            WHERE r.local_path IS NOT NULL AND r.local_path != ''
              AND a.visual_context IS NOT NULL 
              AND a.audio_text IS NOT NULL
            """
        ).fetchall()
        
        count = 0
        for row in rows:
            path = row[0]
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    count += 1
                except Exception as e:
                    logger.error("Failed to delete %s: %s", path, e)
        conn.close()
        if count > 0:
            logger.info("Cleaned up %d processed video files.", count)
    except Exception as e:
        logger.error("Error during video cleanup: %s", e)

def main(tiktok_username: str, batch_limit: int, max_workers: int, skip_to_profiler: bool = False) -> None:
    # Name sanitization — protection against path-traversal
    try:
        tiktok_username = sanitize_username(tiktok_username)
    except ValueError as exc:
        logger.error("Invalid username: %s", exc)
        sys.exit(1)

    target_db = db_path_for(tiktok_username)
    target_report = report_path_for(tiktok_username, "txt")
    pdf_report = report_path_for(tiktok_username, "pdf")

    logger.info("=" * 70)
    logger.info("  START OSINT PIPELINE FOR: @%s", tiktok_username)
    logger.info("  Database: %s | Limit: %d | AI threads: %d", target_db, batch_limit, max_workers)
    logger.info("=" * 70)

    if not skip_to_profiler:
        # ── Step 1: Parsing ───────────────────────────────────────────────────
        logger.info("\n[STEP 1/6] Starting repost parser (CDP)...")
        try:
            tiktok_parser_cdp.parse_fullscreen_feed(tiktok_username, db_path=target_db, limit=batch_limit)
        except Exception as exc:
            logger.error("Parsing error: %s", exc, exc_info=True)
            return

        # ── Step 2: Visual analysis ───────────────────────────────────────────
        logger.info("\n[STEP 2/6] Starting vision analysis (OCR + Moondream)...")
        try:
            vision_manager_3.process_batch(limit=batch_limit, max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Visual analysis error: %s", exc, exc_info=True)
            return

        # ── Step 3: Audio transcription ───────────────────────────────────────
        logger.info("\n[STEP 3/6] Starting audio recognition (Whisper)...")
        try:
            audio_manager.process_audio(limit=batch_limit, db_path=target_db)
        except Exception as exc:
            logger.error("Audio transcription error: %s", exc, exc_info=True)
            return

        # ── Step 4: Sublimation (with visual context) ─────────────────────────
        logger.info("\n[STEP 4/6] Starting Sublimator (Qwen2.5 + multimodal)...")
        try:
            sublimator_cdp.main(max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Sublimation error: %s", exc, exc_info=True)
            return

        # ── Step 5: Fallback analysis (text only) ─────────────────────────────
        logger.info("\n[STEP 5/6] Starting basic analyzer (remaining videos)...")
        try:
            analyzer_cdp.main(max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Fallback analysis error: %s", exc, exc_info=True)
            return

    else:
        if not os.path.exists(target_db):
            logger.error(
                "--only-profiler: database %s not found. Run the full pipeline first.",
                target_db,
            )
            sys.exit(1)
        logger.info("\n[!] Skipped steps 1-5 (--only-profiler mode).")

    # ── Step 6: Profiling ─────────────────────────────────────────────────────
    logger.info("\n[STEP 6/6] Generating final psychological dossier...")
    try:
        profiler_cdp.main(db_path=target_db, output_file=target_report)
    except Exception as exc:
        logger.error("Profiling error: %s", exc, exc_info=True)
        return

    # Video cleanup after full completion (regardless of how profiler finished, though it is after it here)
    cleanup_videos(target_db)

    logger.info("\n" + "=" * 70)
    logger.info("  ALL STAGES SUCCESSFULLY COMPLETED!")
    logger.info("  Text report : %s", target_report)
    logger.info("  PDF dossier : %s", pdf_report)
    logger.info("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Full TikTok OSINT pipeline")
    parser.add_argument("--user",         required=True,        help="TikTok username (without @)")
    parser.add_argument("--limit",        type=int, default=100, help="Video limit for analysis")
    parser.add_argument("--workers",      type=int, default=3,   help="Parallel threads for AI")
    parser.add_argument("--only-profiler", action="store_true",  help="Skip steps 1-5, only generate dossier")
    args = parser.parse_args()

    main(
        tiktok_username=args.user,
        batch_limit=args.limit,
        max_workers=args.workers,
        skip_to_profiler=args.only_profiler,
    )
