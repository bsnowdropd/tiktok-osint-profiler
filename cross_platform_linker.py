"""
cross_platform_linker.py — Search for a username across external platforms.

Changes (refactoring):
  - URL-encode username (urllib.parse.quote) — special characters no longer break the URL
  - allow_redirects=True for all platforms except those where 301/302 = "found"
  - Improved check for Instagram/YouTube/Reddit:
    instead of just status_code 200 — additionally use text_presence for a unique marker
  - Timeout increased to 15s + retry with urllib3 (3 attempts)
  - db_schema.get_connection() to save results
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

# ── HTTP session with retry ────────────────────────────────────────────────────────
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

# ── Platforms ─────────────────────────────────────────────────────────────────
# check: "text_presence" — more reliable method (searches for a unique marker in HTML)
# check: "status_code"   — only for platforms that actually return 404
PLATFORMS: dict[str, dict] = {
    "Instagram": {
        "url": "https://www.instagram.com/{}/",
        "check": "text_presence",
        "marker": '"is_private"',          # present in JSON of all Instagram profiles
    },
    "YouTube": {
        "url": "https://www.youtube.com/@{}",
        "check": "text_presence",
        "marker": '"externalId"',          # present in the meta-tag of the channel page
    },
    "GitHub": {
        "url": "https://github.com/{}",
        "check": "text_presence",
        "marker": 'itemprop="additionalName"',  # missing in GitHub 404 pages
    },
    "Reddit": {
        "url": "https://www.reddit.com/user/{}/about.json",
        "check": "text_presence",
        "marker": '"is_suspended"',         # field in public profile JSON
    },
    "Pinterest": {
        "url": "https://www.pinterest.com/{}/",
        "check": "status_code",            # Pinterest returns 404 for non-existent users
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
    """Checks for the presence of a username on a single platform."""
    # URL-encode protects against special characters in the username
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
        logger.debug("%s: connection error — %s", platform, exc)

    return platform, None


def search_linked_profiles(username: str, max_workers: int = 5) -> dict[str, str]:
    """Parallel search for a username across all platforms."""
    logger.info("Searching for external profiles for: @%s", username)
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
                logger.error("Future error: %s", exc)
                continue
            if url:
                found[platform] = url
                logger.info("Found: %s → %s", platform, url)

    return found


def save_to_db(db_path: str, found_profiles: dict[str, str]) -> None:
    """Saves found profiles into the linked_profiles table."""
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
    logger.info("Saved %d profile(s) to linked_profiles.", len(found_profiles))


if __name__ == "__main__":
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Search for a username across external platforms")
    ap.add_argument("--user", required=True)
    ap.add_argument("--db",   default=None, help="Save results to osint_*.db")
    args = ap.parse_args()

    results = search_linked_profiles(args.user)
    print("\nFound profiles:")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if args.db:
        save_to_db(args.db, results)