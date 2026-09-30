"""
run_pipeline.py — Головний оркестратор OSINT-конвеєра.

Зміни (рефакторинг):
  - username sanitize через utils.sanitize_username (захист path-traversal)
  - використовує utils.db_path_for() для єдиного формату назви файлу БД
  - перевірка наявності БД перед --only-profiler
  - прибрано коментар-артефакт "ТУТ ДОДАНО limit=batch_limit"
  - додано очищення оброблених відео в кінці.
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

# ── Логування ─────────────────────────────────────────────────────────────────
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
    """Видаляє локальні MP4-файли, які були успішно проаналізовані (або позначені як помилкові)."""
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
                    logger.error("Не вдалося видалити %s: %s", path, e)
        conn.close()
        if count > 0:
            logger.info("Очищено %d оброблених відеофайлів.", count)
    except Exception as e:
        logger.error("Помилка під час очищення відео: %s", e)

def main(tiktok_username: str, batch_limit: int, max_workers: int, skip_to_profiler: bool = False) -> None:
    # Санітизація імені — захист від path-traversal
    try:
        tiktok_username = sanitize_username(tiktok_username)
    except ValueError as exc:
        logger.error("Некоректний username: %s", exc)
        sys.exit(1)

    target_db = db_path_for(tiktok_username)
    target_report = report_path_for(tiktok_username, "txt")
    pdf_report = report_path_for(tiktok_username, "pdf")

    logger.info("=" * 70)
    logger.info("  СТАРТ OSINT КОНВЕЄРА ДЛЯ: @%s", tiktok_username)
    logger.info("  База: %s | Ліміт: %d | Потоків ШІ: %d", target_db, batch_limit, max_workers)
    logger.info("=" * 70)

    if not skip_to_profiler:
        # ── Крок 1: Парсинг ───────────────────────────────────────────────────
        logger.info("\n[КРОК 1/6] Запуск парсера репостів (CDP)...")
        try:
            tiktok_parser_cdp.parse_fullscreen_feed(tiktok_username, db_path=target_db, limit=batch_limit)
        except Exception as exc:
            logger.error("Помилка парсингу: %s", exc, exc_info=True)
            return

        # ── Крок 2: Візуальний аналіз ─────────────────────────────────────────
        logger.info("\n[КРОК 2/6] Запуск візійного аналізу (OCR + Moondream)...")
        try:
            vision_manager_3.process_batch(limit=batch_limit, max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Помилка візуального аналізу: %s", exc, exc_info=True)
            return

        # ── Крок 3: Аудіо транскрипція ────────────────────────────────────────
        logger.info("\n[КРОК 3/6] Запуск розпізнавання аудіо (Whisper)...")
        try:
            audio_manager.process_audio(limit=batch_limit, db_path=target_db)
        except Exception as exc:
            logger.error("Помилка аудіо транскрибації: %s", exc, exc_info=True)
            return

        # ── Крок 4: Сублімація (з візуальним контекстом) ──────────────────────
        logger.info("\n[КРОК 4/6] Запуск Субліматора (Qwen2.5 + мультимодальний)...")
        try:
            sublimator_cdp.main(max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Помилка сублімації: %s", exc, exc_info=True)
            return

        # ── Крок 5: Fallback аналіз (тільки текст) ────────────────────────────
        logger.info("\n[КРОК 5/6] Запуск базового аналізатора (залишкові відео)...")
        try:
            analyzer_cdp.main(max_workers=max_workers, db_path=target_db)
        except Exception as exc:
            logger.error("Помилка резервного аналізу: %s", exc, exc_info=True)
            return

    else:
        if not os.path.exists(target_db):
            logger.error(
                "--only-profiler: база %s не знайдена. Спочатку запустіть повний конвеєр.",
                target_db,
            )
            sys.exit(1)
        logger.info("\n[!] Пропущено кроки 1–5 (режим --only-profiler).")

    # ── Крок 6: Профайлінг ────────────────────────────────────────────────────
    logger.info("\n[КРОК 6/6] Генерація фінального психологічного досьє...")
    try:
        profiler_cdp.main(db_path=target_db, output_file=target_report)
    except Exception as exc:
        logger.error("Помилка профайлінгу: %s", exc, exc_info=True)
        return

    # Очищення відео після повного завершення (незалежно від того, як завершився профайлер, хоча тут це після нього)
    cleanup_videos(target_db)

    logger.info("\n" + "=" * 70)
    logger.info("  ВСІ ЕТАПИ УСПІШНО ВИКОНАНО!")
    logger.info("  Текстовий звіт : %s", target_report)
    logger.info("  PDF-досьє      : %s", pdf_report)
    logger.info("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Повний конвеєр TikTok OSINT")
    parser.add_argument("--user",         required=True,        help="Нікнейм TikTok (без @)")
    parser.add_argument("--limit",        type=int, default=100, help="Ліміт відео для аналізу")
    parser.add_argument("--workers",      type=int, default=3,   help="Паралельних потоків для ШІ")
    parser.add_argument("--only-profiler", action="store_true",  help="Пропустити кроки 1–5, лише генерувати досьє")
    args = parser.parse_args()

    main(
        tiktok_username=args.user,
        batch_limit=args.limit,
        max_workers=args.workers,
        skip_to_profiler=args.only_profiler,
    )
