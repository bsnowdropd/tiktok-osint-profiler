"""
migrate_to_postgres.py — Міграція osint_*.db → PostgreSQL (SQLAlchemy 2.x)

РЕЛЯЦІЙНА СХЕМА
───────────────
  users          ← унікальні TikTok-цілі (нікнейм як PK-surrogate)
  videos         ← унікальні TikTok-відео (video_id як PK)
  user_reposts   ← M:N  users ↔ videos + метадані репосту
  analysis       ← LLM-теги, прив'язані до videos (1:1)
  linked_profiles    ← профілі на ін. платформах (FK → users)
  external_footprint ← maigret/instaloader слід (FK → users)

ЧИМ ВІДРІЗНЯЄТЬСЯ ВІД SQLite-СХЕМИ
─────────────────────────────────────
SQLite  : N ізольованих файлів. video_id може дублюватися між файлами.
          Всі репости одного профілю — в одному .db.

PostgreSQL: Єдина БД.
  videos(video_id PK)    — один запис на відео, незалежно від кількості цілей.
  user_reposts(user_id, video_id) UNIQUE — зв'язок M:N, дублікати → ON CONFLICT DO NOTHING.
  Крос-аналіз стає звичайним JOIN замість міжфайлового сканування.

ЗАЛЕЖНОСТІ
──────────
  pip install sqlalchemy>=2.0 psycopg2-binary python-dotenv

ЗАПУСК
──────
  # Налаштуйте змінну середовища або відредагуйте DEFAULT_DSN нижче
  export POSTGRES_DSN="postgresql+psycopg2://user:pass@localhost:5432/osint"

  python migrate_to_postgres.py                            # всі БД
  python migrate_to_postgres.py --dry-run                  # тільки лог
  python migrate_to_postgres.py --source-dir /path/to/dbs
  python migrate_to_postgres.py --dsn postgresql+psycopg2://...
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sqlite3
import sys
from typing import Any

# ── python-dotenv (опційно) ───────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── SQLAlchemy ────────────────────────────────────────────────────────────────
try:
    from sqlalchemy import (
        BigInteger, Boolean, Column, DateTime, ForeignKey,
        Integer, String, Text, UniqueConstraint, create_engine, text,
    )
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.orm import DeclarativeBase, Session, relationship
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_pg")

DEFAULT_DSN   = os.getenv(
    "POSTGRES_DSN",
    "postgresql+psycopg2://osint_user:secret@localhost:5432/osint_db",
)
DEFAULT_BATCH = 500


# ══════════════════════════════════════════════════════════════════════════════
# ORM — Схема PostgreSQL (SQLAlchemy 2.x DeclarativeBase)
# ══════════════════════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    pass


class User(Base):
    """Унікальна TikTok-ціль (нікнейм як природній ключ)."""
    __tablename__ = "users"

    id       = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(128), unique=True, nullable=False, index=True)

    reposts          = relationship("UserRepost", back_populates="user",
                                    cascade="all, delete-orphan")
    linked_profiles  = relationship("LinkedProfile", back_populates="user",
                                    cascade="all, delete-orphan")
    external_footprint = relationship("ExternalFootprint", back_populates="user",
                                      cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User @{self.username}>"


class Video(Base):
    """Унікальне TikTok-відео (video_id як природній PK)."""
    __tablename__ = "videos"

    video_id    = Column(String(64), primary_key=True)
    author      = Column(String(128))
    author_url  = Column(Text)
    description = Column(Text)
    hashtags    = Column(Text)
    sound       = Column(Text)
    url         = Column(Text)

    reposts  = relationship("UserRepost", back_populates="video",
                            cascade="all, delete-orphan")
    analysis = relationship("Analysis", back_populates="video", uselist=False,
                            cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Video {self.video_id}>"


class UserRepost(Base):
    """
    M:N між User і Video + метадані конкретного репосту.
    UNIQUE (user_id, video_id) → ON CONFLICT DO NOTHING при повторному імпорті.
    """
    __tablename__ = "user_reposts"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_user_video"),
    )

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    video_id   = Column(String(64), ForeignKey("videos.video_id", ondelete="CASCADE"),
                        nullable=False, index=True)
    mentions   = Column(Text)
    likes      = Column(String(32))
    comments   = Column(String(32))
    shares     = Column(String(32))
    local_path = Column(Text)
    analyzed   = Column(Integer, default=0)

    user  = relationship("User",  back_populates="reposts")
    video = relationship("Video", back_populates="reposts")


class Analysis(Base):
    """LLM-теги для одного відео (1:1 з Video)."""
    __tablename__ = "analysis"

    video_id       = Column(String(64), ForeignKey("videos.video_id", ondelete="CASCADE"),
                            primary_key=True)
    interests      = Column(Text)
    hobbies        = Column(Text)
    relations      = Column(Text)
    music_taste    = Column(Text)
    raw_result     = Column(Text)
    visual_context = Column(Text)
    video_text     = Column(Text)
    audio_text     = Column(Text)

    video = relationship("Video", back_populates="analysis")


class LinkedProfile(Base):
    """Профіль цілі на ін. платформі."""
    __tablename__ = "linked_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_user_platform"),
    )

    id       = Column(Integer, primary_key=True, autoincrement=True)
    user_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False)
    platform = Column(String(64), nullable=False)
    url      = Column(Text)

    user = relationship("User", back_populates="linked_profiles")


class ExternalFootprint(Base):
    """Цифровий слід (maigret / instaloader)."""
    __tablename__ = "external_footprint"

    id       = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False)
    platform = Column(String(64), nullable=False)
    url      = Column(Text)
    bio_text = Column(Text)

    user = relationship("User", back_populates="external_footprint")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers — читання SQLite
# ══════════════════════════════════════════════════════════════════════════════

def _sqlite_read(db_path: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Відкриває SQLite, читає, одразу закриває → без lock-ів."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _table_exists_sqlite(db_path: str, table: str) -> bool:
    rows = _sqlite_read(
        db_path,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return bool(rows)


def _username_from_path(db_path: str) -> str:
    return os.path.basename(db_path).replace("osint_", "").replace(".db", "")


# ══════════════════════════════════════════════════════════════════════════════
# Клас-мігратор
# ══════════════════════════════════════════════════════════════════════════════

class PostgresMigrator:
    """
    Зчитує N локальних osint_*.db і записує в єдину PostgreSQL.

    Ключові рішення
    ───────────────
    - SQLAlchemy 2.x ORM + Core (pg_insert … ON CONFLICT DO NOTHING)
    - executemany через Session.execute(stmt, [batch]) → ефективно
    - dry_run=True → DDL застосовується, дані НЕ записуються
    """

    def __init__(
        self,
        dsn:     str,
        dry_run: bool = False,
        batch:   int  = DEFAULT_BATCH,
    ) -> None:
        self.dsn     = dsn
        self.dry_run = dry_run
        self.batch   = batch
        self._engine = None

    # ── З'єднання / DDL ───────────────────────────────────────────────────────

    def connect(self) -> None:
        if not SQLALCHEMY_AVAILABLE:
            raise RuntimeError(
                "SQLAlchemy не встановлено.\n"
                "Виконайте: pip install sqlalchemy>=2.0 psycopg2-binary"
            )
        logger.info("Підключення: %s", self.dsn.split("@")[-1])
        self._engine = create_engine(self.dsn, echo=False, future=True)

    def apply_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS для всіх моделей."""
        Base.metadata.create_all(self._engine)
        logger.info("✅ Схему PostgreSQL застосовано")

    def disconnect(self) -> None:
        if self._engine:
            self._engine.dispose()

    # ── Вставка ───────────────────────────────────────────────────────────────

    def _upsert_user(self, session: Session, username: str) -> int:
        """
        INSERT INTO users(username) … ON CONFLICT DO NOTHING;
        SELECT id … — ідемпотентна операція.
        """
        stmt = (
            pg_insert(User)
            .values(username=username)
            .on_conflict_do_nothing(index_elements=["username"])
        )
        session.execute(stmt)
        session.flush()
        user = session.query(User).filter_by(username=username).one()
        return user.id

    def _insert_videos_batch(self, session: Session, reposts: list[dict]) -> None:
        data = [
            {
                "video_id":    r["video_id"],
                "author":      r.get("author", ""),
                "author_url":  r.get("author_url", ""),
                "description": r.get("description", ""),
                "hashtags":    r.get("hashtags", ""),
                "sound":       r.get("sound", ""),
                "url":         r.get("url", ""),
            }
            for r in reposts
            if r.get("video_id")
        ]
        if not data:
            return
        stmt = pg_insert(Video).values(data).on_conflict_do_nothing(
            index_elements=["video_id"]
        )
        session.execute(stmt)

    def _insert_reposts_batch(
        self, session: Session, user_id: int, reposts: list[dict]
    ) -> None:
        data = [
            {
                "user_id":    user_id,
                "video_id":   r["video_id"],
                "mentions":   r.get("mentions", ""),
                "likes":      r.get("likes", ""),
                "comments":   r.get("comments", ""),
                "shares":     r.get("shares", ""),
                "local_path": r.get("local_path", ""),
                "analyzed":   r.get("analyzed", 0),
            }
            for r in reposts
            if r.get("video_id")
        ]
        if not data:
            return
        stmt = pg_insert(UserRepost).values(data).on_conflict_do_nothing(
            constraint="uq_user_video"
        )
        session.execute(stmt)

    def _insert_analysis_batch(self, session: Session, rows: list[dict]) -> None:
        data = [
            {
                "video_id":       r["video_id"],
                "interests":      r.get("interests", ""),
                "hobbies":        r.get("hobbies", ""),
                "relations":      r.get("relations", ""),
                "music_taste":    r.get("music_taste", ""),
                "raw_result":     r.get("raw_result", r.get("summary", "")),
                "visual_context": r.get("visual_context", ""),
                "video_text":     r.get("video_text", ""),
                "audio_text":     r.get("audio_text", ""),
            }
            for r in rows
            if r.get("video_id")
        ]
        if not data:
            return
        stmt = pg_insert(Analysis).values(data).on_conflict_do_nothing(
            index_elements=["video_id"]
        )
        session.execute(stmt)

    def _insert_linked_profiles(
        self, session: Session, user_id: int, rows: list[dict]
    ) -> None:
        data = [
            {
                "user_id":  user_id,
                "platform": r.get("platform", ""),
                "url":      r.get("url", ""),
            }
            for r in rows
        ]
        if not data:
            return
        stmt = pg_insert(LinkedProfile).values(data).on_conflict_do_nothing(
            constraint="uq_user_platform"
        )
        session.execute(stmt)

    # ── Міграція однієї БД ────────────────────────────────────────────────────

    def migrate_one(self, db_path: str) -> dict:
        username = _username_from_path(db_path)
        logger.info("  → @%s  (%s)", username, os.path.basename(db_path))

        # Читаємо all at once → SQLite з'єднання одразу закривається
        reposts  = _sqlite_read(db_path, "SELECT * FROM reposts")
        analysis: list[dict] = []
        linked:   list[dict] = []

        if _table_exists_sqlite(db_path, "analysis"):
            analysis = _sqlite_read(db_path, "SELECT * FROM analysis")
        if _table_exists_sqlite(db_path, "linked_profiles"):
            linked = _sqlite_read(db_path, "SELECT * FROM linked_profiles")

        counts = {
            "username": username,
            "reposts":  len(reposts),
            "analysis": len(analysis),
            "linked":   len(linked),
        }

        if self.dry_run:
            logger.info(
                "    [DRY-RUN] reposts=%d  analysis=%d  linked=%d",
                len(reposts), len(analysis), len(linked),
            )
            return counts

        with Session(self._engine) as session:
            user_id = self._upsert_user(session, username)
            self._insert_videos_batch(session, reposts)
            self._insert_reposts_batch(session, user_id, reposts)
            if analysis:
                self._insert_analysis_batch(session, analysis)
            if linked:
                self._insert_linked_profiles(session, user_id, linked)
            session.commit()

        logger.info(
            "    ✅ reposts=%d  analysis=%d  linked=%d",
            len(reposts), len(analysis), len(linked),
        )
        return counts

    # ── Повна міграція ────────────────────────────────────────────────────────

    def migrate_all(self, source_dir: str) -> list[dict]:
        pattern  = os.path.join(source_dir, "osint_*.db")
        db_files = sorted(glob.glob(pattern))

        if not db_files:
            logger.warning("Не знайдено osint_*.db у %s", source_dir)
            return []

        logger.info("Знайдено %d баз для міграції", len(db_files))
        self.apply_schema()

        results = []
        for db_file in db_files:
            try:
                results.append(self.migrate_one(db_file))
            except Exception as exc:
                logger.error("  ❌ %s: %s", os.path.basename(db_file), exc)

        return results


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Міграція osint_*.db → PostgreSQL (SQLAlchemy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--dsn",        default=DEFAULT_DSN,
                   help="PostgreSQL DSN (за замовчуванням: POSTGRES_DSN env)")
    p.add_argument("--source-dir", default=os.getcwd(),
                   help="Директорія з osint_*.db (за замовчуванням: поточна)")
    p.add_argument("--batch",      type=int, default=DEFAULT_BATCH,
                   help=f"Розмір пакету (за замовчуванням: {DEFAULT_BATCH})")
    p.add_argument("--dry-run",    action="store_true",
                   help="Показати що буде зроблено, без запису у PostgreSQL")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    migrator = PostgresMigrator(
        dsn=args.dsn, dry_run=args.dry_run, batch=args.batch
    )

    if args.dry_run:
        logger.info("=== DRY-RUN — PostgreSQL не буде змінено ===")
        # У dry-run режимі також будуємо engine для перевірки DSN
    try:
        migrator.connect()
    except Exception as exc:
        logger.error("Не вдалося підключитися до PostgreSQL: %s", exc)
        sys.exit(1)

    try:
        results = migrator.migrate_all(args.source_dir)
    finally:
        migrator.disconnect()

    total = sum(r.get("reposts", 0) for r in results)
    logger.info("\n%s", "=" * 60)
    logger.info("  Мігровано профілів : %d", len(results))
    logger.info("  Всього репостів    : %d", total)
    if args.dry_run:
        logger.info("  (DRY-RUN — реальних записів не зроблено)")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
