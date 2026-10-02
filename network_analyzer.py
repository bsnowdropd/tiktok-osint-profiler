"""
network_analyzer.py — Top 5 @-mentions from the DB and their LLM classification.

Changes (refactoring):
  - SQL now filters by `author = username` (previously read the entire DB!)
  - subprocess ollama → requests to HTTP API (aligned with other modules)
  - Model qwen2.5 instead of llama2 (matches configuration)
  - timeout increased to config.OLLAMA_TIMEOUT_TAGS
  - Configuration from config.py
"""

import json
import logging
import sqlite3
from collections import Counter
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


def top_mentions(username: str, db_path: str) -> list[tuple[str, int]]:
    """
    Returns the top 5 @-mentions in reposts of a specific user.

    Args:
        username: TikTok nickname (without '@') — filters only their reposts.
        db_path:  Path to osint_{username}.db.
    """
    try:
        conn = sqlite3.connect(db_path)
        # Filter by author — previously the entire DB was read without a filter
        rows = conn.execute(
            "SELECT mentions FROM reposts WHERE author = ? AND mentions IS NOT NULL",
            (username,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError as exc:
        logger.error("DB query error: %s", exc)
        return []

    mentions: list[str] = []
    for (mentions_json,) in rows:
        try:
            parsed = json.loads(mentions_json)
            if isinstance(parsed, list):
                mentions.extend(parsed)
        except json.JSONDecodeError:
            continue

    return Counter(mentions).most_common(5)


def build_prompt(username: str, top_contacts: list[tuple[str, int]]) -> str:
    """Builds the analytical prompt for Qwen2.5."""
    contacts_str = ", ".join(
        f"{handle} ({cnt} times)" for handle, cnt in top_contacts
    )
    return (
        f"You are an OSINT analyst. Analyze the interactions of TikTok blogger @{username} "
        f"with their top 5 contacts: {contacts_str}.\n"
        "Classify each contact (in English):\n"
        "1. Inner circle — regular shared videos;\n"
        "2. Situational collaborations — streams, challenges;\n"
        "3. Opinion leaders — influencers the blogger reposts.\n"
        "Response — list: nickname → category (1 line)."
    )


def classify_contacts(username: str, db_path: str) -> str:
    """
    Retrieves the top 5 contacts, classifies via Qwen2.5, and returns the text.

    Returns:
        String with the model's response or an error message.
    """
    top = top_mentions(username, db_path)
    if not top:
        return "No @-mentions found in the database for this profile."

    prompt = build_prompt(username, top)

    try:
        res = requests.post(
            config.OLLAMA_URL,
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=config.OLLAMA_TIMEOUT_TAGS,
        )
        if res.status_code == 200:
            return res.json().get("response", "").strip()
        return f"Ollama error: HTTP {res.status_code}"
    except requests.exceptions.Timeout:
        return "Error: timeout for Ollama request."
    except Exception as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")
    ap = argparse.ArgumentParser(description="Classification of top 5 contacts")
    ap.add_argument("--user", required=True, help="TikTok nickname")
    ap.add_argument("--db",   required=True, help="Path to osint_*.db")
    args = ap.parse_args()
    print(classify_contacts(args.user, args.db))
