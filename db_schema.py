"""
db_schema.py — Unified SQLite schema for the entire OSINT pipeline.

Exports `get_connection(db_path)` which automatically initializes
tables if they do not exist.
"""

import sqlite3
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Connects to the database and initializes the schema if it's a new database.
    
    Args:
        db_path: Path to the SQLite database (e.g., databases/osint_username.db)
    Returns:
        Configured sqlite3.Connection object.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    """Creates all necessary tables for the pipeline."""
    c = conn.cursor()

    # 1. Parsed videos (metadata)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reposts (
            video_id TEXT PRIMARY KEY,
            author TEXT,
            url TEXT,
            description TEXT,
            hashtags TEXT,     -- JSON array
            local_path TEXT,   -- Path to downloaded mp4
            parsed_at DATETIME,
            mentions TEXT      -- JSON array of @mentions
        )
    """)

    # 2. AI analysis results
    c.execute("""
        CREATE TABLE IF NOT EXISTS analysis (
            video_id TEXT PRIMARY KEY,
            visual_context TEXT,
            video_text TEXT,
            audio_text TEXT,
            interests TEXT,
            hobbies TEXT,
            relations TEXT,
            music_taste TEXT,
            raw_result TEXT,
            analyzed INTEGER DEFAULT 0,
            FOREIGN KEY(video_id) REFERENCES reposts(video_id)
        )
    """)

    # 3. External platform presence (cross_platform_linker.py)
    c.execute("""
        CREATE TABLE IF NOT EXISTS linked_profiles (
            platform TEXT PRIMARY KEY,
            url TEXT,
            found_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()


@contextmanager
def transaction(db_path: str):
    """
    Context manager for safe transactions.
    Automatically commits on success or rolls back on exception.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("Transaction rolled back due to error: %s", e)
        raise
    finally:
        conn.close()
