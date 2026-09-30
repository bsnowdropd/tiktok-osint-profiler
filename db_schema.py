"""
db_schema.py — Єдина точка ініціалізації схеми SQLite.

Раніше кожен модуль мав свій `init_db_structure()` з різним набором
колонок (конфлікт схем). Тепер одна функція — одна правда про структуру БД.
"""

import sqlite3
import logging

logger = logging.getLogger(__name__)

# ── Повна схема таблиці analysis ─────────────────────────────────────────────
# Будь-яка нова колонка додається ТІЛЬКИ тут.
ANALYSIS_COLUMNS: dict[str, str] = {
    "video_id":       "TEXT PRIMARY KEY",
    "interests":      "TEXT",
    "hobbies":        "TEXT",
    "relations":      "TEXT",
    "music_taste":    "TEXT",
    "raw_result":     "TEXT",          # зберігає summary (1–2 слова)
    "visual_context": "TEXT",
    "video_text":     "TEXT",
    "audio_text":     "TEXT",
    "analyzed_at":    "DATETIME DEFAULT CURRENT_TIMESTAMP",
}


def init_db(conn: sqlite3.Connection) -> None:
    """
    Створює/мігрує всі таблиці бази даних.
    Безпечно: якщо таблиця вже існує — лише додає відсутні колонки.
    """
    # ── reposts ───────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reposts (
            video_id    TEXT PRIMARY KEY,
            author      TEXT,
            author_url  TEXT,
            description TEXT,
            hashtags    TEXT,
            mentions    TEXT,
            sound       TEXT,
            likes       TEXT,
            comments    TEXT,
            shares      TEXT,
            url         TEXT,
            local_path  TEXT,
            parsed_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzed    INTEGER DEFAULT 0
        )
    """)

    # ── analysis ──────────────────────────────────────────────────────────────
    # Будуємо CREATE TABLE з повним списком колонок
    col_defs = ",\n            ".join(
        f"{name} {typedef}" for name, typedef in ANALYSIS_COLUMNS.items()
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS analysis (
            {col_defs},
            FOREIGN KEY (video_id) REFERENCES reposts(video_id)
        )
    """)

    # ── Міграція: додаємо відсутні колонки (ALTER TABLE ADD COLUMN) ───────────
    existing = {row[1] for row in conn.execute("PRAGMA table_info(analysis)").fetchall()}
    for col_name, col_type in ANALYSIS_COLUMNS.items():
        if col_name not in existing and col_name != "video_id":
            # Витягуємо базовий тип без DEFAULT, щоб ALTER TABLE не впав
            base_type = col_type.split()[0]
            logger.info("Міграція БД: додаю колонку analysis.%s (%s)", col_name, base_type)
            conn.execute(f"ALTER TABLE analysis ADD COLUMN {col_name} {base_type}")

    # ── linked_profiles ───────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linked_profiles (
            platform TEXT PRIMARY KEY,
            url      TEXT
        )
    """)

    # ── external_footprint ───────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_footprint (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            platform  TEXT NOT NULL,
            url       TEXT,
            bio_text  TEXT,
            found_at  TEXT NOT NULL
        )
    """)

    conn.commit()
    logger.debug("Схему БД ініціалізовано/перевірено.")


def get_connection(db_path: str) -> sqlite3.Connection:
    """Відкриває з'єднання, застосовує WAL-режим та ініціалізує схему."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")   # краща конкурентність
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn
