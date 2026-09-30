"""
network_analyzer.py — Топ-5 @-згадок із БД та їхня LLM-класифікація.

Зміни (рефакторинг):
  - SQL тепер фільтрує за `author = username` (раніше читав усю базу!)
  - subprocess ollama → requests до HTTP API (узгоджено з рештою модулів)
  - Модель qwen2.5 замість llama2 (відповідає конфігурації)
  - timeout збільшено до config.OLLAMA_TIMEOUT_TAGS
  - Конфігурація з config.py
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
    Повертає топ-5 @-згадок у репостах конкретного користувача.

    Args:
        username: TikTok-нікнейм (без '@') — фільтрує тільки його репости.
        db_path:  Шлях до osint_{username}.db.
    """
    try:
        conn = sqlite3.connect(db_path)
        # Фільтруємо за author — раніше читалася вся база без фільтра
        rows = conn.execute(
            "SELECT mentions FROM reposts WHERE author = ? AND mentions IS NOT NULL",
            (username,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError as exc:
        logger.error("Помилка запиту до БД: %s", exc)
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
    """Формує аналітичний промпт для Qwen2.5."""
    contacts_str = ", ".join(
        f"{handle} ({cnt} разів)" for handle, cnt in top_contacts
    )
    return (
        f"Ти — OSINT-аналітик. Проаналізуй взаємодії TikTok-блогера @{username} "
        f"з його топ-5 контактами: {contacts_str}.\n"
        "Класифікуй кожен контакт (українською):\n"
        "1. Найближче коло — регулярні спільні відео;\n"
        "2. Ситуативні колаборації — стріми, челенджі;\n"
        "3. Лідери думок — інфлюенсери, яких блогер репостить.\n"
        "Відповідь — список: нікнейм → категорія (1 рядок)."
    )


def classify_contacts(username: str, db_path: str) -> str:
    """
    Отримує топ-5 контактів, класифікує через Qwen2.5 і повертає текст.

    Returns:
        Рядок з відповіддю моделі або повідомлення про помилку.
    """
    top = top_mentions(username, db_path)
    if not top:
        return "Не знайдено жодних @-згадок у базі для цього профілю."

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
        return f"Помилка Ollama: HTTP {res.status_code}"
    except requests.exceptions.Timeout:
        return "Помилка: тайм-аут запиту до Ollama."
    except Exception as exc:
        return f"Помилка: {exc}"


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Класифікація топ-5 контактів")
    ap.add_argument("--user", required=True, help="TikTok нікнейм")
    ap.add_argument("--db",   required=True, help="Шлях до osint_*.db")
    args = ap.parse_args()
    print(classify_contacts(args.user, args.db))
