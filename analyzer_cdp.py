"""
analyzer_cdp.py — Fallback LLM-analysis (step 5): text-only Qwen2.5.

Processes videos left after the Sublimator (analyzed=0, without visual context).

Changes (refactoring):
  - _safe_str → utils.safe_str (duplicate removed)
  - init_db_structure → db_schema.get_connection()
  - Configuration from config.py
  - executor.map → futures collecting as they finish (do not keep all in RAM)
  - Retry-logic for Ollama (3 attempts)
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
_RETRY_DELAY = 5


def _call_ollama_with_retry(payload: dict, timeout: int) -> dict | None:
    """Sends a request to Ollama with retry."""
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            res = requests.post(config.OLLAMA_URL, json=payload, timeout=timeout)
            if res.status_code == 200:
                return res.json()
            logger.warning("Ollama HTTP %d (attempt %d/%d)", res.status_code, attempt, _RETRY_COUNT)
        except requests.exceptions.Timeout:
            logger.warning("Ollama timeout (attempt %d/%d)", attempt, _RETRY_COUNT)
        except Exception as exc:
            logger.error("Ollama error: %s", exc)
        if attempt < _RETRY_COUNT:
            time.sleep(_RETRY_DELAY)
    return None


def analyze_repost_fallback(data: dict) -> tuple[str, dict] | None:
    """Basic video analysis (no visual context) — text and sound only."""
    v_id   = data.get("video_id", "unknown")
    desc   = data.get("description", "")
    sound  = data.get("sound", "")
    author = data.get("author", "unknown")

    prompt = f"""
    Extract BRIEF key tags from a TikTok repost.
    There is no visual description — rely only on text, hashtags, and sound.

    Context:
    - Author: @{author}
    - Description/Hashtags: {desc}
    - Sound Name: {sound}

    RULES:
    1. interests/hobbies: direct facts only ("dancing", "programming").
    2. music_taste: only if the video is about music/clip.
    3. relations: only if it's about relationships/family/friendship.
    4. summary: 1-2 words ("humor", "motivation").

    JSON (in English):
    {{
      "interests": "tag1, tag2",
      "hobbies": "",
      "relations": "",
      "music_taste": "",
      "summary": "category"
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
        return v_id, json.loads(raw["response"])
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Failed to parse response for %s: %s", v_id, exc)
        return None


def main(max_workers: int = 3, db_path: str = "osint_unknown.db") -> None:
    conn = get_connection(db_path)

    rows = conn.execute(
        "SELECT * FROM reposts WHERE analyzed = 0"
    ).fetchall()

    if not rows:
        logger.info("No remaining videos for basic analysis.")
        conn.close()
        return

    logger.info("Basic analysis of %d videos in %d threads...", len(rows), max_workers)
    tasks = [dict(row) for row in rows]

    # Collect results as they are ready (do not keep all in RAM at the same time)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(analyze_repost_fallback, t): t["video_id"] for t in tasks}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if not res:
                continue
            v_id, result = res
            logger.info(
                "Analysis %s → %s | %s",
                v_id, result.get("interests", ""), result.get("summary", ""),
            )
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
            conn.commit()   # commit one by one — if the process crashes, we won't lose what's already saved

    conn.close()
    logger.info("Basic analysis completed!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")
    main(max_workers=3)