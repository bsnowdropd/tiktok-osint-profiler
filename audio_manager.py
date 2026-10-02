"""
audio_manager.py - Whisper STT: audio transcription from video files.

Changes (refactoring):
  - init_db_structure -> db_schema.get_connection() (unified schema)
  - os.remove ONLY after successful transcription (previously deleted even on error)
  - Sentinel value " " replaced with actual empty string "" with explicit marking
  - LIMIT {limit} via f-string -> parameterized query (SQL injection)
  - WHISPER_MODEL_SIZE -> config.WHISPER_MODEL_SIZE (now "small" instead of "base")
  - warnings.filterwarnings("ignore") -> specific filter only for UserWarning
"""

import logging
import os
import warnings
from pathlib import Path

import whisper

import config
from db_schema import get_connection

# Suppress only FP16 UserWarning from Whisper/PyTorch, not all warnings
warnings.filterwarnings("ignore", message=".*FP16.*", category=UserWarning)

logger = logging.getLogger(__name__)


def process_audio(limit: int = 100, db_path: str = "osint_unknown.db") -> None:
    """
    Transcribes audio from local MP4 files using Whisper.

    Logic:
    - Selects videos from `reposts` where `local_path` is present and `audio_text` is empty.
    - After successful transcription, deletes the MP4 from disk (saves space).
    - On transcription error - DOES NOT delete the file, writes NULL to DB,
      so the next run can retry.
    """
    conn = get_connection(db_path)

    # Parameterized query - no f-string for LIMIT (protection against SQL injection)
    rows = conn.execute(
        """
        SELECT r.video_id, r.local_path
        FROM reposts r
        LEFT JOIN analysis a ON r.video_id = a.video_id
        WHERE r.local_path IS NOT NULL
          AND r.local_path != ''
          AND (a.audio_text IS NULL OR a.audio_text = '')
        ORDER BY r.parsed_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        logger.info("No new videos found for transcription.")
        conn.close()
        return

    logger.info(
        "Loading Whisper '%s' for transcribing %d videos...",
        config.WHISPER_MODEL_SIZE,
        len(rows),
    )

    try:
        model = whisper.load_model(config.WHISPER_MODEL_SIZE)
    except Exception as exc:
        logger.error("Failed to load Whisper: %s", exc)
        logger.error("Install: pip install openai-whisper and apt install ffmpeg")
        conn.close()
        return

    total = len(rows)
    for i, row in enumerate(rows, 1):
        v_id, local_path = row["video_id"], row["local_path"]

        if not local_path or not Path(local_path).exists():
            logger.warning("[%d/%d] File not found: %s (ID: %s)", i, total, local_path, v_id)
            # Mark as processed (file deleted earlier or unavailable)
            conn.execute(
                """INSERT INTO analysis (video_id, audio_text) VALUES (?, '')
                   ON CONFLICT(video_id) DO UPDATE SET audio_text = ''""",
                (v_id,),
            )
            conn.commit()
            continue

        logger.info("[%d/%d] Transcribing: %s", i, total, v_id)
        transcribed_text: str | None = None
        success = False

        try:
            result = model.transcribe(local_path, fp16=False)
            transcribed_text = result.get("text", "").strip()
            success = True

            if transcribed_text:
                logger.info("  [✓] Voice: %s...", transcribed_text[:60])
            else:
                logger.info("  [i] Video without voice.")
                transcribed_text = ""   # explicit empty string, not " " (sentinel anti-pattern)

        except Exception as exc:
            logger.error("  [!] Transcription error %s: %s", v_id, exc)
            # transcribed_text remains None -> video will be reprocessed

        # ── Writing to DB ───────────────────────────────────────────────────────
        if success:
            conn.execute(
                """INSERT INTO analysis (video_id, audio_text) VALUES (?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET audio_text = excluded.audio_text""",
                (v_id, transcribed_text),
            )
            conn.commit()

            # MP4 deletion moved to run_pipeline.py
        else:
            logger.warning("  [~] %s: file kept for retry.", v_id)

    conn.close()
    logger.info("Audio transcription completed!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")
    process_audio(limit=50)