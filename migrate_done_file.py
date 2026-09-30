"""
migrate_done_file.py — Міграція старих БД із папки done_file

Що робить:
  1. Сканує done_file/ на наявність osint_*.db
  2. Копіює кожну БД у корінь проєкту (якщо ще не скопійована)
  3. Застосовує поточну схему db_schema.py (додає відсутні колонки)
  4. Витягує @-згадки з поля `description` та пише у нову колонку `mentions`

Запуск:
    python migrate_done_file.py
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import logging
import sys

# Додаємо поточну директорію у PYTHONPATH для імпорту db_schema
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")

# ── Шляхи ────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DONE_DIR = os.path.join(PROJECT_DIR, "done_file")
DB_DIR = os.path.join(PROJECT_DIR, "databases")
os.makedirs(DB_DIR, exist_ok=True)

_AT_RE = re.compile(r"@([\w.]+)")


def extract_mentions(text: str) -> str:
    """Витягує всі @нікнейми з тексту та повертає JSON-список."""
    if not text:
        return "[]"
    found = _AT_RE.findall(text)
    unique = list(dict.fromkeys(found))   # зберігаємо порядок, без дублікатів
    return json.dumps(unique, ensure_ascii=False)


def migrate_reposts_mentions(conn: sqlite3.Connection) -> int:
    """
    Заповнює колонку mentions у reposts на основі description.
    Повертає кількість оновлених рядків.
    """
    # Перевіряємо чи колонка вже існує
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reposts)").fetchall()}
    if "mentions" not in existing:
        logger.warning("  Колонка mentions відсутня — пропускаємо (мала бути додана init_db)")
        return 0

    rows = conn.execute(
        "SELECT video_id, description FROM reposts WHERE mentions IS NULL OR mentions = ''"
    ).fetchall()

    count = 0
    for video_id, description in rows:
        mentions_json = extract_mentions(description or "")
        conn.execute(
            "UPDATE reposts SET mentions = ? WHERE video_id = ?",
            (mentions_json, video_id),
        )
        count += 1

    conn.commit()
    return count


def migrate_analysis_columns(conn: sqlite3.Connection) -> None:
    """
    Зводить стару таблицю analysis до поточної схеми:
    - Перейменовує legacy-колонки (summary → raw_result якщо потрібно)
    - Додає відсутні нові колонки
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(analysis)").fetchall()}

    # Legacy: old DBs had `summary` instead of `raw_result`
    # SQLite не вміє ALTER COLUMN RENAME → створимо новий стовпець і скопіюємо дані
    if "summary" in existing and "raw_result" not in existing:
        logger.info("  Міграція: копіюю summary → raw_result")
        conn.execute("ALTER TABLE analysis ADD COLUMN raw_result TEXT")
        conn.execute("UPDATE analysis SET raw_result = summary")
        conn.commit()
        existing.add("raw_result")

    # Нові обов'язкові колонки
    for col_name, col_type in db_schema.ANALYSIS_COLUMNS.items():
        if col_name == "video_id":
            continue
        if col_name not in existing:
            base_type = col_type.split()[0]
            logger.info("  Додаю колонку analysis.%s (%s)", col_name, base_type)
            conn.execute(f"ALTER TABLE analysis ADD COLUMN {col_name} {base_type}")

    conn.commit()


def add_reposts_mentions_column(conn: sqlite3.Connection) -> None:
    """Додає колонку mentions до reposts якщо відсутня."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reposts)").fetchall()}
    if "mentions" not in existing:
        logger.info("  Додаю колонку reposts.mentions")
        conn.execute("ALTER TABLE reposts ADD COLUMN mentions TEXT")
        conn.commit()


def add_missing_linked_tables(conn: sqlite3.Connection) -> None:
    """Додає linked_profiles та external_footprint якщо відсутні."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "linked_profiles" not in tables:
        logger.info("  Створюю таблицю linked_profiles")
        conn.execute("""
            CREATE TABLE linked_profiles (
                platform TEXT PRIMARY KEY,
                url      TEXT
            )
        """)

    if "external_footprint" not in tables:
        logger.info("  Створюю таблицю external_footprint")
        conn.execute("""
            CREATE TABLE external_footprint (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                platform  TEXT NOT NULL,
                url       TEXT,
                bio_text  TEXT,
                found_at  TEXT NOT NULL
            )
        """)

    conn.commit()


def migrate_one(src_path: str) -> None:
    """Мігрує одну БД: копіює у проєкт та оновлює схему."""
    filename = os.path.basename(src_path)
    dest_path = os.path.join(DB_DIR, filename)

    # ── 1. Копіюємо (якщо ще немає або джерело новіше) ───────────────────────
    if not os.path.exists(dest_path):
        shutil.copy2(src_path, dest_path)
        logger.info("✅ Скопійовано: %s → databases/%s", filename, filename)
    else:
        logger.info("⏩ Файл вже існує у проєкті: %s (пропускаємо копіювання)", filename)

    # ── 2. Відкриваємо та мігруємо ────────────────────────────────────────────
    logger.info("🔧 Мігрую схему: %s", filename)
    conn = sqlite3.connect(dest_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")   # вимкнено для ALTER TABLE

    try:
        add_reposts_mentions_column(conn)
        migrate_analysis_columns(conn)
        add_missing_linked_tables(conn)

        updated = migrate_reposts_mentions(conn)
        logger.info("  Заповнено mentions: %d рядків", updated)
    except Exception as exc:
        logger.error("  ❌ Помилка міграції %s: %s", filename, exc)
    finally:
        conn.close()


def main() -> None:
    if not os.path.isdir(DONE_DIR):
        logger.error("Папка done_file не знайдена: %s", DONE_DIR)
        sys.exit(1)

    db_files = sorted(glob.glob(os.path.join(DONE_DIR, "osint_*.db")))
    if not db_files:
        logger.warning("У done_file не знайдено osint_*.db файлів.")
        return

    logger.info("Знайдено %d БД для міграції:", len(db_files))
    for p in db_files:
        logger.info("  • %s", os.path.basename(p))

    print()
    for db_path in db_files:
        migrate_one(db_path)
        print()

    logger.info("🎉 Міграцію завершено. Перезапустіть Streamlit.")


if __name__ == "__main__":
    main()
