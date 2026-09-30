"""
db_manager.py — DB Abstraction Layer (Repository Pattern)

Єдина точка доступу до даних для всього OSINT-дашборду.

Архітектурне рішення
────────────────────
Замість розсипаних `sqlite3.connect(...)` по всьому коду, уся логіка
запитів зосереджена тут. При переході на PostgreSQL достатньо:
  1. Змінити _connect() → повернути psycopg2/asyncpg з'єднання.
  2. Поправити плейсхолдери: SQLite використовує ?, PostgreSQL — %s.
  3. Більше нічого у решті проєкту не чіпати.

Клас DatabaseManager
────────────────────
- Відкриває з'єднання тільки на час конкретного запиту і одразу
  закриває → жодних «database is locked» при паралельному Streamlit.
- Всі публічні методи повертають чисті Python-структури (list[dict] /
  list[tuple] / int), без sqlite3.Row або інших DB-специфічних типів.
- Кожен метод — це окремий «репозиторій-запит» (один метод = одна
  відповідальність), що полегшує тестування та мокування.

Використання
────────────
    from db_manager import DatabaseManager

    mgr = DatabaseManager("/abs/path/to/osint_user.db")
    reposts = mgr.get_reposts()          # list[dict]
    vid_ids = mgr.get_video_ids()        # list[str]
    count   = mgr.count_reposts()        # int
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Any

logger = logging.getLogger(__name__)

# ── Типові аліаси ─────────────────────────────────────────────────────────────
Row  = dict[str, Any]
Rows = list[Row]


class DatabaseManager:
    """
    Адаптер доступу до SQLite-бази одного OSINT-профілю.

    Parameters
    ----------
    db_path : str | Path
        Абсолютний або відносний шлях до файлу .db.

    Notes
    -----
    Для переходу на PostgreSQL:
        - Замініть `sqlite3.connect` на `psycopg2.connect(dsn=...)`
        - Замініть `?` на `%s` у всіх SQL-рядках
        - row_factory більше не потрібен — psycopg2.extras.RealDictCursor
          повертає dict напряму
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    # ── Внутрішній менеджер контексту ─────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Відкриває з'єднання, налаштовує WAL + row_factory,
        гарантовано закриває після блоку.

        PostgreSQL-замінник:
            conn = psycopg2.connect(dsn=self.db_path)
            conn.cursor_factory = psycopg2.extras.RealDictCursor
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row   # доступ за іменем колонки
        try:
            yield conn
        finally:
            conn.close()

    def _rows_to_dicts(self, rows: list[sqlite3.Row]) -> Rows:
        """Конвертує sqlite3.Row → plain dict для незалежності від драйвера."""
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════════════════
    # РЕПОСТИ
    # ═══════════════════════════════════════════════════════════════════════════

    def get_reposts(
        self,
        limit: int | None = None,
        order_by: str = "parsed_at DESC",
    ) -> Rows:
        """
        Повертає всі репости з таблиці reposts.

        Returns
        -------
        list[dict] з ключами: video_id, author, author_url, description,
            hashtags, mentions, sound, likes, comments, shares, url,
            local_path, parsed_at, analyzed
        """
        sql = f"SELECT * FROM reposts ORDER BY {order_by}"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def get_video_ids(self) -> list[str]:
        """Повертає лише video_id всіх репостів."""
        with self._connect() as conn:
            rows = conn.execute("SELECT video_id FROM reposts").fetchall()
        return [r["video_id"] for r in rows]

    def get_video_ids_with_urls(self) -> list[tuple[str, str]]:
        """
        Повертає пари (video_id, url) для крос-аналізу.
        url може бути None — обробляємо як порожній рядок.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT video_id, COALESCE(url, '') AS url FROM reposts"
            ).fetchall()
        return [(r["video_id"], r["url"]) for r in rows]

    def get_video_ids_with_author(self) -> list[tuple[str, str, str]]:
        """
        Повертає трійки (video_id, url, author) для крос-аналізу.
        Потрібен для побудови DataFrame формату Maltego (Source/Target).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT video_id, COALESCE(url, '') AS url, "
                "COALESCE(author, '') AS author FROM reposts"
            ).fetchall()
        return [(r["video_id"], r["url"], r["author"]) for r in rows]

    def get_reposts_with_mentions(self) -> Rows:
        """Повертає лише ті репости, де є @-згадки (не порожній JSON)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT author, mentions FROM reposts "
                "WHERE mentions IS NOT NULL AND mentions != '[]'"
            ).fetchall()
        return self._rows_to_dicts(rows)

    def count_reposts(self) -> int:
        """Загальна кількість рядків у reposts."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM reposts").fetchone()[0]

    def count_mentions(self) -> int:
        """Кількість відео з @-згадками."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM reposts "
                "WHERE mentions IS NOT NULL AND mentions != '[]'"
            ).fetchone()[0]

    # ═══════════════════════════════════════════════════════════════════════════
    # АНАЛІЗ
    # ═══════════════════════════════════════════════════════════════════════════

    def get_analysis_context(self, limit: int = 50) -> Rows:
        """
        JOIN reposts + analysis для RAG-чату.
        Повертає лише рядки де є хоча б якийсь аналіз (interests != NULL).
        """
        sql = """
            SELECT r.author, r.description,
                   a.interests, a.hobbies, a.relations,
                   a.music_taste, a.audio_text
            FROM reposts r
            LEFT JOIN analysis a ON r.video_id = a.video_id
            WHERE a.interests IS NOT NULL
            ORDER BY r.parsed_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return self._rows_to_dicts(rows)

    def get_evidence(self, limit: int = 30) -> Rows:
        """
        Дані для вкладки «Доказова база»: відео + OCR + транскрипція.
        """
        sql = """
            SELECT r.video_id, r.author, r.url,
                   a.visual_context, a.video_text, a.audio_text
            FROM reposts r
            LEFT JOIN analysis a ON r.video_id = a.video_id
            WHERE a.visual_context IS NOT NULL OR a.video_text IS NOT NULL
            ORDER BY r.parsed_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return self._rows_to_dicts(rows)

    # ═══════════════════════════════════════════════════════════════════════════
    # СЛУЖБОВІ / МЕТАДАНІ
    # ═══════════════════════════════════════════════════════════════════════════

    def table_exists(self, table_name: str) -> bool:
        """Перевіряє наявність таблиці у БД."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        return row is not None

    def get_linked_profiles(self) -> Rows:
        """Повертає знайдені профілі на інших платформах."""
        if not self.table_exists("linked_profiles"):
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM linked_profiles").fetchall()
        return self._rows_to_dicts(rows)

    def get_external_footprint(self) -> Rows:
        """Повертає цифровий слід (maigret / instaloader)."""
        if not self.table_exists("external_footprint"):
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_footprint ORDER BY found_at DESC"
            ).fetchall()
        return self._rows_to_dicts(rows)

    def __repr__(self) -> str:
        return f"DatabaseManager(db='{self.db_path}')"
