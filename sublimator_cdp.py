"""
sublimator_cdp.py — Глибокий LLM-аналіз (Qwen2.5) репостів з візуальним контекстом.

Зміни (рефакторинг):
  - _safe_str → імпорт з utils (прибрано дублікат)
  - init_db_structure → db_schema.get_connection()
  - needed_columns розширено до повного списку (раніше пропускав visual_context, video_text)
  - `visual_context IS NOT NULL` → `COALESCE(a.visual_context, '') != ''`
    (ловить порожні рядки від Moondream, які раніше пропускалися)
  - raw_result тепер зберігає summary (назва поля відповідає змісту)
  - Конфігурація з config.py
  - Retry-логіка для Ollama (3 спроби з паузою)
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
_RETRY_DELAY = 5  # секунд між спробами


def _call_ollama_with_retry(payload: dict, timeout: int) -> dict | None:
    """Надсилає запит до Ollama з retry-логікою (3 спроби)."""
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            res = requests.post(config.OLLAMA_URL, json=payload, timeout=timeout)
            if res.status_code == 200:
                return res.json()
            logger.warning("Ollama HTTP %d (спроба %d/%d)", res.status_code, attempt, _RETRY_COUNT)
        except requests.exceptions.Timeout:
            logger.warning("Ollama тайм-аут (спроба %d/%d)", attempt, _RETRY_COUNT)
        except Exception as exc:
            logger.error("Ollama помилка (спроба %d/%d): %s", attempt, _RETRY_COUNT, exc)

        if attempt < _RETRY_COUNT:
            time.sleep(_RETRY_DELAY)

    return None


def analyze_repost(data: dict) -> tuple[str, dict] | None:
    """Глибокий аналіз репосту: OCR + Moondream + Whisper + метадані → теги."""
    v_id    = data.get("video_id", "unknown")
    desc    = data.get("description", "")
    sound   = data.get("sound", "")
    author  = data.get("author", "")
    visual  = data.get("visual_context", "")
    ocr     = data.get("video_text", "")
    audio   = data.get("audio_text", "")

    prompt = f"""
    Витягни СТИСЛІ ключові теги з репосту TikTok. 
    Пріоритет: транскрипція > OCR > візуальний опис > текст/хештеги.

    ВХІДНІ ДАНІ:
    - Текст на екрані (OCR): {ocr}
    - Візуальна хронологія (3 кадри): {visual}
    - Голосова транскрипція: {audio}
    - Опис/Хештеги: {desc}
    - Назва звуку: {sound}
    - Автор: @{author}

    ПРАВИЛА:
    1. interests/hobbies: тільки прямі факти ("авто", "психологія", "геймінг").
    2. music_taste: заповнювати ТІЛЬКИ якщо відео про музику/кліп.
    3. relations: тільки якщо відео про стосунки/сім'ю/дружбу.
    4. summary: 1–2 слова — суть відео ("мем", "туторіал", "влог").

    JSON (українською):
    {{
      "interests": "тег1, тег2",
      "hobbies": "тег1",
      "relations": "",
      "music_taste": "",
      "summary": "суть"
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
        logger.error("Не вдалося розпарсити відповідь Ollama для %s: %s", v_id, exc)
        return None


def main(max_workers: int = 3, db_path: str = "osint_unknown.db") -> None:
    conn = get_connection(db_path)

    # COALESCE ловить NULL і "" — обидва випадки "відсутній контекст"
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
        logger.info("Немає відео з візуальним контекстом для сублімації.")
        conn.close()
        return

    logger.info("Сублімація %d відео у %d потоках...", len(rows), max_workers)
    tasks = [dict(row) for row in rows]
    results: list[tuple[str, dict]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(analyze_repost, tasks):
            if res:
                results.append(res)
                v_id, data = res
                logger.info(
                    "Сублімовано %s → %s | %s",
                    v_id, data.get("interests", ""), data.get("summary", ""),
                )

    if results:
        logger.info("Збереження %d результатів...", len(results))
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
    logger.info("Сублімація завершена!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main(max_workers=3)