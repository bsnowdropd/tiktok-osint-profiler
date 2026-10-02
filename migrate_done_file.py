"""
migrate_done_file.py — Migration of old DBs from the done_file folder

What it does:
  1. Scans done_file/ for osint_*.db
  2. Copies each DB to the project root (if not already copied)
  3. Applies the current db_schema.py schema (adds missing columns)
  4. Extracts @-mentions from the `description` field and writes to the new `mentions` column

Execution:
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

# Adding current directory to PYTHONPATH to import db_schema
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_schema

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DONE_DIR = os.path.join(PROJECT_DIR, "done_file")
DB_DIR = os.path.join(PROJECT_DIR, "databases")
os.makedirs(DB_DIR, exist_ok=True)

_AT_RE = re.compile(r"@([\w.]+)")


def extract_mentions(text: str) -> str:
    """Extracts all @nicknames from the text and returns a JSON list."""
    if not text:
        return "[]"
    found = _AT_RE.findall(text)
    unique = list(dict.fromkeys(found))   # preserve order, no duplicates
    return json.dumps(unique, ensure_ascii=False)


def migrate_reposts_mentions(conn: sqlite3.Connection) -> int:
    """
    Fills the mentions column in reposts based on description.
    Returns the number of updated rows.
    """
    # Check if the column already exists
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reposts)").fetchall()}
    if "mentions" not in existing:
        logger.warning("  Column mentions is missing — skipping (should have been added by init_db)")
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
    Brings the old analysis table to the current schema:
    - Renames legacy columns (summary → raw_result if necessary)
    - Adds missing new columns
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(analysis)").fetchall()}

    # Legacy: old DBs had `summary` instead of `raw_result`
    # SQLite does not support ALTER COLUMN RENAME → create a new column and copy data
    if "summary" in existing and "raw_result" not in existing:
        logger.info("  Migration: copying summary → raw_result")
        conn.execute("ALTER TABLE analysis ADD COLUMN raw_result TEXT")
        conn.execute("UPDATE analysis SET raw_result = summary")
        conn.commit()
        existing.add("raw_result")

    # New required columns
    for col_name, col_type in db_schema.ANALYSIS_COLUMNS.items():
        if col_name == "video_id":
            continue
        if col_name not in existing:
            base_type = col_type.split()[0]
            logger.info("  Adding column analysis.%s (%s)", col_name, base_type)
            conn.execute(f"ALTER TABLE analysis ADD COLUMN {col_name} {base_type}")

    conn.commit()


def add_reposts_mentions_column(conn: sqlite3.Connection) -> None:
    """Adds mentions column to reposts if missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reposts)").fetchall()}
    if "mentions" not in existing:
        logger.info("  Adding column reposts.mentions")
        conn.execute("ALTER TABLE reposts ADD COLUMN mentions TEXT")
        conn.commit()


def add_missing_linked_tables(conn: sqlite3.Connection) -> None:
    """Adds linked_profiles and external_footprint if missing."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "linked_profiles" not in tables:
        logger.info("  Creating table linked_profiles")
        conn.execute("""
            CREATE TABLE linked_profiles (
                platform TEXT PRIMARY KEY,
                url      TEXT
            )
        """)

    if "external_footprint" not in tables:
        logger.info("  Creating table external_footprint")
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
    """Migrates a single DB: copies to the project and updates schema."""
    filename = os.path.basename(src_path)
    dest_path = os.path.join(DB_DIR, filename)

    # ── 1. Copy (if not present or source is newer) ───────────────────────
    if not os.path.exists(dest_path):
        shutil.copy2(src_path, dest_path)
        logger.info("✅ Copied: %s → databases/%s", filename, filename)
    else:
        logger.info("⏩ File already exists in project: %s (skipping copy)", filename)

    # ── 2. Open and migrate ────────────────────────────────────────────
    logger.info("🔧 Migrating schema: %s", filename)
    conn = sqlite3.connect(dest_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")   # disabled for ALTER TABLE

    try:
        add_reposts_mentions_column(conn)
        migrate_analysis_columns(conn)
        add_missing_linked_tables(conn)

        updated = migrate_reposts_mentions(conn)
        logger.info("  Filled mentions: %d rows", updated)
    except Exception as exc:
        logger.error("  ❌ Migration error %s: %s", filename, exc)
    finally:
        conn.close()


def main() -> None:
    if not os.path.isdir(DONE_DIR):
        logger.error("Folder done_file not found: %s", DONE_DIR)
        sys.exit(1)

    db_files = sorted(glob.glob(os.path.join(DONE_DIR, "osint_*.db")))
    if not db_files:
        logger.warning("No osint_*.db files found in done_file.")
        return

    logger.info("Found %d DBs for migration:", len(db_files))
    for p in db_files:
        logger.info("  • %s", os.path.basename(p))

    print()
    for db_path in db_files:
        migrate_one(db_path)
        print()

    logger.info("🎉 Migration completed. Please restart Streamlit.")


if __name__ == "__main__":
    main()
