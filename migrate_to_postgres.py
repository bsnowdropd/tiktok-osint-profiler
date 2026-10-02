"""
migrate_to_postgres.py — Migration osint_*.db → PostgreSQL (SQLAlchemy 2.x)

RELATIONAL SCHEMA
───────────────
  users          ← unique TikTok targets (nickname as PK-surrogate)
  videos         ← unique TikTok videos (video_id as PK)
  user_reposts   ← M:N  users ↔ videos + repost metadata
  analysis       ← LLM tags, tied to videos (1:1)
  linked_profiles    ← profiles on other platforms (FK → users)
  external_footprint ← maigret/instaloader trace (FK → users)

HOW IT DIFFERS FROM SQLite SCHEMA
─────────────────────────────────────
SQLite  : N isolated files. video_id can be duplicated between files.
          All reposts of one profile are in a single .db.

PostgreSQL: Single DB.
  videos(video_id PK)    — one record per video, regardless of targets count.
  user_reposts(user_id, video_id) UNIQUE — M:N relation, duplicates → ON CONFLICT DO NOTHING.
  Cross-analysis becomes a regular JOIN instead of cross-file scanning.

DEPENDENCIES
──────────
  pip install sqlalchemy>=2.0 psycopg2-binary python-dotenv

EXECUTION
──────
  # Configure environment variable or edit DEFAULT_DSN below
  export POSTGRES_DSN="postgresql+psycopg2://user:pass@localhost:5432/osint"

  python migrate_to_postgres.py                            # all DBs
  python migrate_to_postgres.py --dry-run                  # log only
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

# ── python-dotenv (optional) ───────────────────────────────────────────────────
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
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_pg")

DEFAULT_DSN   = os.getenv(
    "POSTGRES_DSN",
    "postgresql+psycopg2://osint_user:secret@localhost:5432/osint_db",
)
DEFAULT_BATCH = 500


# ══════════════════════════════════════════════════════════════════════════════
# ORM — PostgreSQL Schema (SQLAlchemy 2.x DeclarativeBase)
# ══════════════════════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    pass


class User(Base):
    """Unique TikTok target (nickname as natural key)."""
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
    """Unique TikTok video (video_id as natural PK)."""
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
    M:N between User and Video + metadata of a specific repost.
    UNIQUE (user_id, video_id) → ON CONFLICT DO NOTHING on re-import.
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
    """LLM tags for a single video (1:1 with Video)."""
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
    """Target's profile on another platform."""
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
    """Digital footprint (maigret / instaloader)."""
    __tablename__ = "external_footprint"

    id       = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False)
    platform = Column(String(64), nullable=False)
    url      = Column(Text)
    bio_text = Column(Text)

    user = relationship("User", back_populates="external_footprint")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers — reading SQLite
# ══════════════════════════════════════════════════════════════════════════════

def _sqlite_read(db_path: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Opens SQLite, reads, immediately closes → without locks."""
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
# Migrator class
# ══════════════════════════════════════════════════════════════════════════════

class PostgresMigrator:
    """
    Reads N local osint_*.db and writes to a single PostgreSQL.

    Key decisions
    ───────────────
    - SQLAlchemy 2.x ORM + Core (pg_insert … ON CONFLICT DO NOTHING)
    - executemany via Session.execute(stmt, [batch]) → efficiently
    - dry_run=True → DDL applied, data NOT written
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

    # ── Connection / DDL ───────────────────────────────────────────────────────

    def connect(self) -> None:
        if not SQLALCHEMY_AVAILABLE:
            raise RuntimeError(
                "SQLAlchemy is not installed.\n"
                "Run: pip install sqlalchemy>=2.0 psycopg2-binary"
            )
        logger.info("Connecting: %s", self.dsn.split("@")[-1])
        self._engine = create_engine(self.dsn, echo=False, future=True)

    def apply_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS for all models."""
        Base.metadata.create_all(self._engine)
        logger.info("✅ PostgreSQL schema applied")

    def disconnect(self) -> None:
        if self._engine:
            self._engine.dispose()

    # ── Insertion ───────────────────────────────────────────────────────────────

    def _upsert_user(self, session: Session, username: str) -> int:
        """
        INSERT INTO users(username) … ON CONFLICT DO NOTHING;
        SELECT id … — idempotent operation.
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

    # ── Single DB Migration ────────────────────────────────────────────────────

    def migrate_one(self, db_path: str) -> dict:
        username = _username_from_path(db_path)
        logger.info("  → @%s  (%s)", username, os.path.basename(db_path))

        # Reading all at once → SQLite connection closes immediately
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

    # ── Full migration ────────────────────────────────────────────────────────

    def migrate_all(self, source_dir: str) -> list[dict]:
        pattern  = os.path.join(source_dir, "osint_*.db")
        db_files = sorted(glob.glob(pattern))

        if not db_files:
            logger.warning("No osint_*.db found in %s", source_dir)
            return []

        logger.info("Found %d databases for migration", len(db_files))
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
        description="Migration osint_*.db → PostgreSQL (SQLAlchemy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--dsn",        default=DEFAULT_DSN,
                   help="PostgreSQL DSN (default: POSTGRES_DSN env)")
    p.add_argument("--source-dir", default=os.getcwd(),
                   help="Directory with osint_*.db (default: current)")
    p.add_argument("--batch",      type=int, default=DEFAULT_BATCH,
                   help=f"Batch size (default: {DEFAULT_BATCH})")
    p.add_argument("--dry-run",    action="store_true",
                   help="Show what will be done, without writing to PostgreSQL")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    migrator = PostgresMigrator(
        dsn=args.dsn, dry_run=args.dry_run, batch=args.batch
    )

    if args.dry_run:
        logger.info("=== DRY-RUN — PostgreSQL will not be changed ===")
        # In dry-run mode we also build engine to check DSN
    try:
        migrator.connect()
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL: %s", exc)
        sys.exit(1)

    try:
        results = migrator.migrate_all(args.source_dir)
    finally:
        migrator.disconnect()

    total = sum(r.get("reposts", 0) for r in results)
    logger.info("\n%s", "=" * 60)
    logger.info("  Migrated profiles : %d", len(results))
    logger.info("  Total reposts     : %d", total)
    if args.dry_run:
        logger.info("  (DRY-RUN — no real writes performed)")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
