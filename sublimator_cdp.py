"""
sublimator_cdp.py — Deep LLM-analysis (Qwen2.5) of reposts with visual context.

Changes (refactoring):
  - _safe_str → import from utils (duplicate removed)
  - init_db_structure → db_schema.get_connection()
  - needed_columns expanded to the full list (previously skipped visual_context, video_text)
  - `visual_context IS NOT NULL` → `COALESCE(a.visual_context, '') != ''`
    (catches empty strings from Moondream, which were skipped before)
  - raw_result now stores summary (field name matches content)
  - Configuration from config.py
  - Retry-logic for Ollama (3 attempts with pause)
"""

import json
import logging
import time
import concurrent.futures

import requests

import config
from db_schema import get_connection
from utils import safe_str

logger = logging.getLogger(__name__)

_RETRY_COUNT = 3
_RETRY_DELAY = 5  # seconds between attempts


def _call_ollama_with_retry(payload: dict, timeout: int) -> dict | None:
    """Sends a request to Ollama with retry-logic (3 attempts)."""
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            res = requests.post(config.OLLAMA_URL, json=payload, timeout=timeout)
            if res.status_code == 200:
                return res.json()
            logger.warning("Ollama HTTP %d (attempt %d/%d)", res.status_code, attempt, _RETRY_COUNT)
        except requests.exceptions.Timeout:
            logger.warning("Ollama timeout (attempt %d/%d)", attempt, _RETRY_COUNT)
        except Exception as exc:
            logger.error("Ollama error (attempt %d/%d): %s", attempt, _RETRY_COUNT, exc)

        if attempt < _RETRY_COUNT:
            time.sleep(_RETRY_DELAY)

    return None


def analyze_repost(data: dict) -> tuple[str, dict] | None:
    """Deep repost analysis: OCR + Moondream + Whisper + metadata → tags."""
    v_id    = data.get("video_id", "unknown")
    desc    = data.get("description", "")
    sound   = data.get("sound", "")
    author  = data.get("author", "")
    visual  = data.get("visual_context", "")
    ocr     = data.get("video_text", "")
    audio   = data.get("audio_text", "")

    prompt = f"""
    Extract BRIEF key tags from a TikTok repost. 
    Priority: transcription > OCR > visual description > text/hashtags.

    INPUT DATA:
    - On-screen text (OCR): {ocr}
    - Visual timeline (3 frames): {visual}
    - Voice transcription: {audio}
    - Description/Hashtags: {desc}
    - Sound Name: {sound}
    - Author: @{author}

    RULES:
    1. interests/hobbies: direct facts only ("cars", "psychology", "gaming").
    2. music_taste: fill ONLY if the video is about music/clip.
    3. relations: only if the video is about relationships/family/friendship.
    4. summary: 1-2 words — essence of the video ("meme", "tutorial", "vlog").

    JSON (in English):
    {{
      "interests": "tag1, tag2",
      "hobbies": "tag1",
      "relations": "",
      "music_taste": "",
      "summary": "essence"
    }}
    """

    raw = _call_ollama_with_retry(
        {
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        },
        timeout=config.OLLAMA_TIMEOUT_TAGS,
    )

    if raw is None:
        return None

    try:
        parsed = json.loads(raw["response"])
        return v_id, parsed
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Failed to parse Ollama response for %s: %s", v_id, exc)
        return None


def main(max_workers: int = 3, db_path: str = "osint_unknown.db") -> None:
    conn = get_connection(db_path)

    # COALESCE catches NULL and "" — both are "missing context" cases
    rows = conn.execute(
        """
        SELECT r.*, a.visual_context, a.video_text, a.audio_text
        FROM reposts r
        LEFT JOIN analysis a ON r.video_id = a.video_id
        WHERE r.analyzed = 0
          AND COALESCE(a.visual_context, '') != ''
        """
    ).fetchall()

    if not rows:
        logger.info("No videos with visual context for sublimation.")
        conn.close()
        return

    logger.info("Sublimation of %d videos in %d threads...", len(rows), max_workers)
    tasks = [dict(row) for row in rows]
    results: list[tuple[str, dict]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(analyze_repost, tasks):
            if res:
                results.append(res)
                v_id, data = res
                logger.info(
                    "Sublimated %s → %s | %s",
                    v_id, data.get("interests", ""), data.get("summary", ""),
                )

    if results:
        logger.info("Saving %d results...", len(results))
        for v_id, result in results:
            conn.execute(
                """
                INSERT INTO analysis (video_id, interests, hobbies, relations, music_taste, raw_result)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    interests   = excluded.interests,
                    hobbies     = excluded.hobbies,
                    relations   = excluded.relations,
                    music_taste = excluded.music_taste,
                    raw_result  = excluded.raw_result
                """,
                (
                    v_id,
                    safe_str(result.get("interests", "")),
                    safe_str(result.get("hobbies", "")),
                    safe_str(result.get("relations", "")),
                    safe_str(result.get("music_taste", "")),
                    safe_str(result.get("summary", "")),
                ),
            )
            conn.execute("UPDATE reposts SET analyzed = 1 WHERE video_id = ?", (v_id,))
        conn.commit()

    conn.close()
    logger.info("Sublimation completed!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")
    main(max_workers=3)