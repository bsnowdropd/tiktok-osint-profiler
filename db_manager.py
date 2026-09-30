"""
db_manager.py — Repository pattern for unified database access.
"""

import json
import logging
import sqlite3
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from utils import db_path_for

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Provides methods to access and modify the SQLite database."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Returns a configured SQLite connection with check_same_thread=False."""
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initializes tables using db_schema."""
        from db_schema import get_connection
        conn = get_connection(self.db_path)
        conn.close()

    # ── Reposts (Parser) ──────────────────────────────────────────────────────

    def save_repost(self, data: Dict[str, Any]) -> None:
        """Saves parsed video metadata into the database."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO reposts (
                    video_id, author, url, description, 
                    hashtags, mentions, local_path, parsed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    description = excluded.description,
                    hashtags = excluded.hashtags,
                    mentions = excluded.mentions,
                    local_path = excluded.local_path
            """, (
                data["video_id"],
                data.get("author", ""),
                data.get("url", ""),
                data.get("description", ""),
                json.dumps(data.get("hashtags", [])),
                json.dumps(data.get("mentions", [])),
                data.get("local_path", ""),
                data.get("parsed_at", "")
            ))

    def get_unprocessed_videos(self, limit: int = 100) -> List[sqlite3.Row]:
        """Returns videos that have not yet been processed by the AI pipeline."""
        with self._connect() as conn:
            return conn.execute("""
                SELECT r.video_id, r.local_path, r.description, r.hashtags
                FROM reposts r
                LEFT JOIN analysis a ON r.video_id = a.video_id
                WHERE a.video_id IS NULL OR a.analyzed = 0
                LIMIT ?
            """, (limit,)).fetchall()

    # ── Analysis (AI Pipeline) ────────────────────────────────────────────────

    def save_vision_text(self, video_id: str, visual_context: str, ocr_text: str) -> None:
        """Saves computer vision results (frames description and OCR)."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO analysis (video_id, visual_context, video_text)
                VALUES (?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    visual_context = excluded.visual_context,
                    video_text = excluded.video_text
            """, (video_id, visual_context, ocr_text))

    def save_audio_text(self, video_id: str, audio_text: str) -> None:
        """Saves Whisper transcription results."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO analysis (video_id, audio_text)
                VALUES (?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    audio_text = excluded.audio_text
            """, (video_id, audio_text))

    def save_semantic_tags(self, video_id: str, tags_data: Dict[str, Any], raw_result: str) -> None:
        """Saves parsed semantic tags from Qwen and marks video as fully analyzed."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO analysis (
                    video_id, interests, hobbies, relations, music_taste, raw_result, analyzed
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(video_id) DO UPDATE SET
                    interests = excluded.interests,
                    hobbies = excluded.hobbies,
                    relations = excluded.relations,
                    music_taste = excluded.music_taste,
                    raw_result = excluded.raw_result,
                    analyzed = 1
            """, (
                video_id,
                json.dumps(tags_data.get("interests", [])),
                json.dumps(tags_data.get("hobbies", [])),
                json.dumps(tags_data.get("relations", [])),
                json.dumps(tags_data.get("music_taste", [])),
                raw_result
            ))

    def mark_analyzed(self, video_id: str, raw_result: str = "") -> None:
        """Marks video as analyzed (used as fallback when tags are empty)."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO analysis (video_id, raw_result, analyzed)
                VALUES (?, ?, 1)
                ON CONFLICT(video_id) DO UPDATE SET
                    raw_result = excluded.raw_result,
                    analyzed = 1
            """, (video_id, raw_result))

    # ── Retrieval (Cross-Analysis & Graph) ────────────────────────────────────

    def get_reposts_with_mentions(self) -> List[sqlite3.Row]:
        """Returns all reposts that have valid @mentions."""
        with self._connect() as conn:
            return conn.execute("""
                SELECT author, mentions 
                FROM reposts 
                WHERE mentions IS NOT NULL AND mentions != '[]'
            """).fetchall()

    def get_video_ids_with_author(self) -> List[sqlite3.Row]:
        """Returns all video IDs alongside their author (target user)."""
        with self._connect() as conn:
            return conn.execute("""
                SELECT video_id, author 
                FROM reposts
            """).fetchall()

    def get_all_analysis_data(self) -> List[sqlite3.Row]:
        """Returns fully analyzed AI records for report generation."""
        with self._connect() as conn:
            return conn.execute("""
                SELECT r.description, r.hashtags, 
                       a.visual_context, a.video_text, a.audio_text, 
                       a.interests, a.hobbies, a.relations, a.music_taste, a.raw_result
                FROM reposts r
                LEFT JOIN analysis a ON r.video_id = a.video_id
                WHERE a.analyzed = 1
            """).fetchall()

    def get_stats(self) -> Dict[str, int]:
        """Returns basic database statistics."""
        with self._connect() as conn:
            total_reposts = conn.execute("SELECT COUNT(*) FROM reposts").fetchone()[0]
            analyzed = conn.execute("SELECT COUNT(*) FROM analysis WHERE analyzed = 1").fetchone()[0]
            return {
                "total_reposts": total_reposts,
                "analyzed": analyzed
            }
