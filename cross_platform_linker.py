"""
cross_platform_linker.py — Пошук нікнейму на зовнішніх платформах.

Зміни (рефакторинг):
  - URL-encode нікнейму (urllib.parse.quote) — спецсимволи більше не ламають URL
  - allow_redirects=True для всіх платформ окрім тих, де 301/302 = "знайдено"
  - Покращена перевірка для Instagram/YouTube/Reddit:
    замість лише status_code 200 — додатково text_presence для унікального маркера
  - Таймаут збільшено до 15 с + retry з urllib3 (3 спроби)
  - db_schema.get_connection() для збереження результатів
"""

import concurrent.futures
import logging
import sqlite3
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db_schema import get_connection

logger = logging.getLogger(__name__)

# ── HTTP-сесія з retry ────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
_adapter = HTTPAdapter(max_retries=_retry_strategy)
_session = requests.Session()
_session.headers.update(_HEADERS)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# ── Платформи ─────────────────────────────────────────────────────────────────
# check: "text_presence" — надійніший метод (шукає унікальний маркер у HTML)
# check: "status_code"   — лише для платформ, що дійсно повертають 404
PLATFORMS: dict[str, dict] = {
    "Instagram": {
        "url": "https://www.instagram.com/{}/",
        "check": "text_presence",
        "marker": '"is_private"',          # наявний у JSON усіх Instagram-профілів
    },
    "YouTube": {
        "url": "https://www.youtube.com/@{}",
        "check": "text_presence",
        "marker": '"externalId"',          # є в meta-тезі сторінки каналу
    },
    "GitHub": {
        "url": "https://github.com/{}",
        "check": "text_presence",
        "marker": 'itemprop="additionalName"',  # відсутній у 404-сторінках GitHub
    },
    "Reddit": {
        "url": "https://www.reddit.com/user/{}/about.json",
        "check": "text_presence",
        "marker": '"is_suspended"',         # поле у JSON публічного профілю
    },
    "Pinterest": {
        "url": "https://www.pinterest.com/{}/",
        "check": "status_code",            # Pinterest повертає 404 для неіснуючих
        "redirects": True,
    },
    "Snapchat": {
        "url": "https://www.snapchat.com/add/{}",
        "check": "text_presence",
        "marker": '"displayName"',
    },
    "Twitch": {
        "url": "https://www.twitch.tv/{}",
        "check": "text_presence",
        "marker": '"@type":"Person"',
    },
    "Telegram": {
        "url": "https://t.me/{}",
        "check": "text_presence",
        "marker": "tgme_page_extra",
    },
}


def check_single_platform(
    username: str, platform: str, cfg: dict
) -> tuple[str, str | None]:
    """Перевіряє наявність нікнейму на одній платформі."""
    # URL-encode захищає від спецсимволів у нікнеймі
    safe_username = quote(username, safe="")
    url = cfg["url"].format(safe_username)
    follow_redirects = cfg.get("redirects", True)

    try:
        res = _session.get(url, timeout=15, allow_redirects=follow_redirects)

        if cfg["check"] == "status_code":
            if res.status_code == 200:
                return platform, url

        elif cfg["check"] == "text_presence":
            marker = cfg.get("marker", "")
            if res.status_code == 200 and marker and marker in res.text:
                return platform, url

    except requests.exceptions.RequestException as exc:
        logger.debug("%s: помилка з'єднання — %s", platform, exc)

    return platform, None


def search_linked_profiles(username: str, max_workers: int = 5) -> dict[str, str]:
    """Паралельний пошук нікнейму на всіх платформах."""
    logger.info("Пошук зовнішніх профілів для: @%s", username)
    found: dict[str, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(check_single_platform, username, platform, cfg): platform
            for platform, cfg in PLATFORMS.items()
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                platform, url = future.result()
            except Exception as exc:
                logger.error("Помилка майбутнього: %s", exc)
                continue
            if url:
                found[platform] = url
                logger.info("Знайдено: %s → %s", platform, url)

    return found


def save_to_db(db_path: str, found_profiles: dict[str, str]) -> None:
    """Зберігає знайдені профілі у таблицю linked_profiles."""
    if not found_profiles:
        return
    conn = get_connection(db_path)
    for platform, url in found_profiles.items():
        conn.execute(
            """
            INSERT INTO linked_profiles (platform, url) VALUES (?, ?)
            ON CONFLICT(platform) DO UPDATE SET url = excluded.url
            """,
            (platform, url),
        )
    conn.commit()
    conn.close()
    logger.info("Збережено %d профіль(ів) у linked_profiles.", len(found_profiles))


if __name__ == "__main__":
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Пошук нікнейму на зовнішніх платформах")
    ap.add_argument("--user", required=True)
    ap.add_argument("--db",   default=None, help="Зберегти результати у osint_*.db")
    args = ap.parse_args()

    results = search_linked_profiles(args.user)
    print("\nЗнайдені профілі:")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if args.db:
        save_to_db(args.db, results)