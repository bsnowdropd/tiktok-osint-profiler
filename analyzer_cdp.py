"""
analyzer_cdp.py — Fallback LLM-аналіз (крок 5): text-only Qwen2.5.

Обробляє відео, що залишилися після Субліматора (analyzed=0, без візуального контексту).

Зміни (рефакторинг):
  - _safe_str → utils.safe_str (прибрано дублікат)
  - init_db_structure → db_schema.get_connection()
  - Конфігурація з config.py
  - executor.map → futures зі збором по мірі готовності (не тримаємо всі в RAM)
  - Retry-логіка для Ollama (3 спроби)
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
    """Надсилає запит до Ollama з retry."""
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            res = requests.post(config.OLLAMA_URL, json=payload, timeout=timeout)
            if res.status_code == 200:
                return res.json()
            logger.warning("Ollama HTTP %d (спроба %d/%d)", res.status_code, attempt, _RETRY_COUNT)
        except requests.exceptions.Timeout:
            logger.warning("Ollama тайм-аут (спроба %d/%d)", attempt, _RETRY_COUNT)
        except Exception as exc:
            logger.error("Ollama помилка: %s", exc)
        if attempt < _RETRY_COUNT:
            time.sleep(_RETRY_DELAY)
    return None


def analyze_repost_fallback(data: dict) -> tuple[str, dict] | None:
    """Базовий аналіз відео (без візуального контексту) — тільки текст та звук."""
    v_id   = data.get("video_id", "unknown")
    desc   = data.get("description", "")
    sound  = data.get("sound", "")
    author = data.get("author", "unknown")

    prompt = f"""
    Витягни СТИСЛІ ключові теги з репосту TikTok.
    Візуального опису немає — орієнтуйся лише на текст, хештеги та звук.

    Контекст:
    - Автор: @{author}
    - Опис/Хештеги: {desc}
    - Назва звуку: {sound}

    ПРАВИЛА:
    1. interests/hobbies: тільки прямі факти ("танці", "програмування").
    2. music_taste: тільки якщо відео про музику/кліп.
    3. relations: тільки якщо про стосунки/сім'ю/дружбу.
    4. summary: 1–2 слова ("гумор", "мотивація").

    JSON (українською):
    {{
      "interests": "тег1, тег2",
      "hobbies": "",
      "relations": "",
      "music_taste": "",
      "summary": "категорія"
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
        logger.error("Розпарсити відповідь для %s не вдалося: %s", v_id, exc)
        return None


def main(max_workers: int = 3, db_path: str = "osint_unknown.db") -> None:
    conn = get_connection(db_path)

    rows = conn.execute(
        "SELECT * FROM reposts WHERE analyzed = 0"
    ).fetchall()

    if not rows:
        logger.info("Немає залишкових відео для базового аналізу.")
        conn.close()
        return

    logger.info("Базовий аналіз %d відео у %d потоках...", len(rows), max_workers)
    tasks = [dict(row) for row in rows]

    # Збираємо результати по мірі готовності (не тримаємо всі у RAM одночасно)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(analyze_repost_fallback, t): t["video_id"] for t in tasks}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if not res:
                continue
            v_id, result = res
            logger.info(
                "Аналіз %s → %s | %s",
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
            conn.commit()   # комітимо по одному — якщо процес впаде, не втратимо вже збережене

    conn.close()
    logger.info("Базовий аналіз завершено!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main(max_workers=3)