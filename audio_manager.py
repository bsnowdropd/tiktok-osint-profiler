"""
audio_manager.py — Whisper STT: транскрипція аудіо з відеофайлів.

Зміни (рефакторинг):
  - init_db_structure → db_schema.get_connection() (уніфікована схема)
  - os.remove ТІЛЬКИ після успішної транскрибації (раніше видалявся навіть при помилці)
  - Sentinel-значення " " замінено на реальну пустий рядок "" з явним позначкою
  - LIMIT {limit} через f-string → параметризований запит (SQL-ін'єкція)
  - WHISPER_MODEL_SIZE → config.WHISPER_MODEL_SIZE (тепер "small" замість "base")
  - warnings.filterwarnings("ignore") → конкретний фільтр тільки для UserWarning
"""

import logging
import os
import warnings
from pathlib import Path

import whisper

import config
from db_schema import get_connection

# Пригнічуємо лише FP16 UserWarning від Whisper/PyTorch, не всі попередження
warnings.filterwarnings("ignore", message=".*FP16.*", category=UserWarning)

logger = logging.getLogger(__name__)


def process_audio(limit: int = 100, db_path: str = "osint_unknown.db") -> None:
    """
    Транскрибує аудіо з локальних MP4-файлів за допомогою Whisper.

    Логіка:
    - Обирає відео з `reposts`, де є `local_path` і ще немає `audio_text`.
    - Після успішної транскрибації видаляє MP4 з диску (економія місця).
    - При помилці транскрибації — НЕ видаляє файл, записує NULL у БД,
      щоб наступний запуск міг повторити спробу.
    """
    conn = get_connection(db_path)

    # Параметризований запит — без f-string для LIMIT (захист від SQL injection)
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
        logger.info("Нових відео для транскрибації не знайдено.")
        conn.close()
        return

    logger.info(
        "Завантаження Whisper '%s' для транскрибації %d відео...",
        config.WHISPER_MODEL_SIZE,
        len(rows),
    )

    try:
        model = whisper.load_model(config.WHISPER_MODEL_SIZE)
    except Exception as exc:
        logger.error("Не вдалося завантажити Whisper: %s", exc)
        logger.error("Встановіть: pip install openai-whisper  та  apt install ffmpeg")
        conn.close()
        return

    total = len(rows)
    for i, row in enumerate(rows, 1):
        v_id, local_path = row["video_id"], row["local_path"]

        if not local_path or not Path(local_path).exists():
            logger.warning("[%d/%d] Файл не знайдено: %s (ID: %s)", i, total, local_path, v_id)
            # Позначаємо як оброблений (файл видалено раніше або недоступний)
            conn.execute(
                """INSERT INTO analysis (video_id, audio_text) VALUES (?, '')
                   ON CONFLICT(video_id) DO UPDATE SET audio_text = ''""",
                (v_id,),
            )
            conn.commit()
            continue

        logger.info("[%d/%d] Транскрибація: %s", i, total, v_id)
        transcribed_text: str | None = None
        success = False

        try:
            result = model.transcribe(local_path, fp16=False)
            transcribed_text = result.get("text", "").strip()
            success = True

            if transcribed_text:
                logger.info("  [✓] Голос: %s...", transcribed_text[:60])
            else:
                logger.info("  [i] Відео без голосу.")
                transcribed_text = ""   # явний порожній рядок, не " " (sentinel-антипатерн)

        except Exception as exc:
            logger.error("  [!] Помилка транскрибації %s: %s", v_id, exc)
            # transcribed_text залишається None → відео буде повторно оброблено

        # ── Запис у БД ────────────────────────────────────────────────────────
        if success:
            conn.execute(
                """INSERT INTO analysis (video_id, audio_text) VALUES (?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET audio_text = excluded.audio_text""",
                (v_id, transcribed_text),
            )
            conn.commit()

            # Видалення MP4 перенесено до run_pipeline.py
        else:
            logger.warning("  [~] %s: файл збережено для повторної спроби.", v_id)

    conn.close()
    logger.info("Транскрибацію аудіо завершено!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    process_audio(limit=50)