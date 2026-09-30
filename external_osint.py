"""
external_osint.py — Асинхронний пошук цифрового сліду через maigret + instaloader.

Зміни (рефакторинг):
  - tempfile.mktemp() → tempfile.NamedTemporaryFile (усунуто TOCTOU race condition)
  - executemany з ON CONFLICT DO NOTHING (усунуто дублікати при повторному запуску)
  - asyncio.get_event_loop() → asyncio.get_running_loop() (Python 3.10+ сумісність)
  - return_exceptions=True у gather (один збій не обвалює весь пайплайн)
  - logging.basicConfig перенесено у if __name__ == "__main__"
  - Instagram зберігається у БД тільки якщо bio непорожнє або профіль знайдений
  - db_schema.get_connection() замість прямого sqlite3.connect()
"""

# ── pip install maigret instaloader aiohttp ───────────────────────────────────

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from db_schema import get_connection

logger = logging.getLogger(__name__)

# ── Перевірка наявності залежностей ──────────────────────────────────────────
try:
    import maigret          # noqa: F401  (перевірка наявності)
    MAIGRET_AVAILABLE = True
except ImportError:
    MAIGRET_AVAILABLE = False

try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False


# ── Збереження у БД ───────────────────────────────────────────────────────────

def save_footprint_records(
    username: str,
    records: list[tuple[str, str, str]],
) -> None:
    """Зберігає список (platform, url, bio_text) у external_footprint."""
    if not records:
        return

    from utils import db_path_for
    db_path = db_path_for(username)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection(db_path)
    # ON CONFLICT DO NOTHING — повторний запуск не дублює записи
    conn.executemany(
        """
        INSERT INTO external_footprint (platform, url, bio_text, found_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [(platform, url, bio_text, now) for platform, url, bio_text in records],
    )
    conn.commit()
    conn.close()
    logger.info("Збережено %d записів у external_footprint.", len(records))


# ── Maigret (пошук на 3000+ сайтах) ──────────────────────────────────────────

async def search_with_maigret(username: str) -> list[tuple[str, str, str]]:
    """
    Запускає maigret через CLI (asyncio subprocess) і парсить JSON-результат.
    Повертає список (platform, url, "") для знайдених профілів.
    """
    results: list[tuple[str, str, str]] = []

    if not MAIGRET_AVAILABLE:
        logger.warning("maigret не встановлено (pip install maigret). Крок пропущено.")
        return results

    logger.info("Запускаємо maigret для @%s...", username)

    # NamedTemporaryFile замість небезпечного mktemp (усуває TOCTOU)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)

    try:
        proc = await asyncio.create_subprocess_exec(
            "maigret", username,
            "--no-color",
            "--json", str(out_path),
            "--timeout", "30",
            "--max-connections", "50",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("maigret: загальний тайм-аут 300 с.")
            return results

        if out_path.exists():
            import json as _json
            try:
                data = _json.loads(out_path.read_text(encoding="utf-8"))
            except _json.JSONDecodeError as exc:
                logger.error("maigret: некоректний JSON — %s", exc)
                return results

            user_data = data.get(username, data)
            for site_name, site_info in user_data.items():
                status = site_info.get("status", {})
                if isinstance(status, dict):
                    found = status.get("status") in ("Claimed", "found", "Found")
                elif isinstance(status, str):
                    found = status in ("Claimed", "found", "Found")
                else:
                    found = False

                if found:
                    url = site_info.get("url_user") or site_info.get("url", "")
                    results.append((site_name, url, ""))

    except FileNotFoundError:
        logger.error("Команда `maigret` не знайдена. Встановіть: pip install maigret")
    except Exception as exc:
        logger.error("maigret: неочікувана помилка — %s", exc)
    finally:
        out_path.unlink(missing_ok=True)

    logger.info("maigret: знайдено %d профіль(ів).", len(results))
    return results


# ── Instaloader (Instagram bio) ───────────────────────────────────────────────

async def fetch_instagram_bio(username: str) -> tuple[str, str, str]:
    """
    Анонімно отримує bio Instagram-профілю.
    Повертає ("Instagram", url, bio_text).
    """
    platform = "Instagram"
    url = f"https://www.instagram.com/{username}/"

    if not INSTALOADER_AVAILABLE:
        logger.warning("instaloader не встановлено (pip install instaloader). Крок пропущено.")
        return platform, url, ""

    logger.info("Запит Instagram bio для @%s...", username)

    def _sync_fetch() -> str:
        il = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            request_timeout=20,
        )
        profile = instaloader.Profile.from_username(il.context, username)
        return profile.biography or ""

    try:
        # get_running_loop() замість get_event_loop() (Python 3.10+ сумісність)
        loop = asyncio.get_running_loop()
        bio = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=45,
        )
        logger.info("Instagram bio: %d символів.", len(bio))
        return platform, url, bio

    except asyncio.TimeoutError:
        logger.warning("Instagram: тайм-аут.")
    except instaloader.exceptions.ProfileNotExistsException:
        logger.info("Instagram: профіль @%s не знайдено.", username)
    except instaloader.exceptions.LoginRequiredException:
        logger.warning("Instagram: профіль закритий, потрібен логін.")
    except instaloader.exceptions.ConnectionException as exc:
        logger.warning("Instagram: помилка з'єднання — %s", exc)
    except Exception as exc:
        logger.warning("Instagram: неочікувана помилка — %s", exc)

    return platform, url, ""


# ── Точка входу ───────────────────────────────────────────────────────────────

async def run_external_osint(username: str) -> str:
    """
    Паралельно запускає maigret та instaloader, зберігає у external_footprint.

    Returns:
        Рядок-зведення для відображення у Streamlit або CLI.
    """
    logger.info("=== external_osint START: @%s ===", username)

    # return_exceptions=True — збій одного джерела не зупиняє інше
    maigret_result, instagram_result = await asyncio.gather(
        search_with_maigret(username),
        fetch_instagram_bio(username),
        return_exceptions=True,
    )

    # Обробляємо можливі винятки з gather
    maigret_records: list[tuple[str, str, str]] = []
    if isinstance(maigret_result, Exception):
        logger.error("Maigret завершився з помилкою: %s", maigret_result)
    else:
        maigret_records = maigret_result

    ig_platform, ig_url, ig_bio = "Instagram", f"https://www.instagram.com/{username}/", ""
    if isinstance(instagram_result, Exception):
        logger.error("Instaloader завершився з помилкою: %s", instagram_result)
    else:
        ig_platform, ig_url, ig_bio = instagram_result

    # Зберігаємо у БД тільки реальні знахідки
    all_records = list(maigret_records)
    if ig_bio:   # зберігаємо Instagram тільки якщо є bio (профіль дійсно знайдено)
        all_records.append((ig_platform, ig_url, ig_bio))

    save_footprint_records(username, all_records)

    # ── Зведення ──────────────────────────────────────────────────────────────
    count = len(maigret_records)
    if count > 0:
        preview = ", ".join(r[0] for r in maigret_records[:10])
        if count > 10:
            preview += f" та ще {count - 10} інших"
        summary = f"Знайдено {count} профіль(ів) на зовнішніх платформах: {preview}."
    else:
        summary = "maigret не знайшов збігів на зовнішніх платформах."

    if ig_bio:
        summary += f"\nInstagram bio (@{username}): {ig_bio[:300]}"
    elif INSTALOADER_AVAILABLE:
        summary += f"\nInstagram (@{username}): профіль не знайдено або закрито."

    logger.info("=== external_osint DONE: @%s ===", username)
    return summary


if __name__ == "__main__":
    import sys
    # logging.basicConfig — тільки у точці входу
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s – %(message)s")

    if len(sys.argv) < 2:
        print("Використання: python external_osint.py <username>")
        sys.exit(1)

    target = sys.argv[1].lstrip("@")
    result = asyncio.run(run_external_osint(target))
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)
