"""
external_osint.py — Asynchronous digital footprint search via maigret + instaloader.

Changes (refactoring):
  - tempfile.mktemp() → tempfile.NamedTemporaryFile (eliminated TOCTOU race condition)
  - executemany with ON CONFLICT DO NOTHING (eliminated duplicates on rerun)
  - asyncio.get_event_loop() → asyncio.get_running_loop() (Python 3.10+ compatibility)
  - return_exceptions=True in gather (one failure doesn't crash the whole pipeline)
  - logging.basicConfig moved to if __name__ == "__main__"
  - Instagram is saved to DB only if bio is not empty or profile is found
  - db_schema.get_connection() instead of direct sqlite3.connect()
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

# ── Dependency check ──────────────────────────────────────────
try:
    import maigret          # noqa: F401  (check availability)
    MAIGRET_AVAILABLE = True
except ImportError:
    MAIGRET_AVAILABLE = False

try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False


# ── Saving to DB ───────────────────────────────────────────────────────────

def save_footprint_records(
    username: str,
    records: list[tuple[str, str, str]],
) -> None:
    """Saves a list of (platform, url, bio_text) to external_footprint."""
    if not records:
        return

    from utils import db_path_for
    db_path = db_path_for(username)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection(db_path)
    # ON CONFLICT DO NOTHING — reruns do not duplicate records
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
    logger.info("Saved %d records to external_footprint.", len(records))


# ── Maigret (search on 3000+ sites) ──────────────────────────────────────────

async def search_with_maigret(username: str) -> list[tuple[str, str, str]]:
    """
    Runs maigret via CLI (asyncio subprocess) and parses the JSON result.
    Returns a list of (platform, url, "") for found profiles.
    """
    results: list[tuple[str, str, str]] = []

    if not MAIGRET_AVAILABLE:
        logger.warning("maigret is not installed (pip install maigret). Step skipped.")
        return results

    logger.info("Running maigret for @%s...", username)

    # NamedTemporaryFile instead of unsafe mktemp (eliminates TOCTOU)
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
            logger.error("maigret: general timeout of 300 s.")
            return results

        if out_path.exists():
            import json as _json
            try:
                data = _json.loads(out_path.read_text(encoding="utf-8"))
            except _json.JSONDecodeError as exc:
                logger.error("maigret: invalid JSON — %s", exc)
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
        logger.error("Command `maigret` not found. Install: pip install maigret")
    except Exception as exc:
        logger.error("maigret: unexpected error — %s", exc)
    finally:
        out_path.unlink(missing_ok=True)

    logger.info("maigret: found %d profile(s).", len(results))
    return results


# ── Instaloader (Instagram bio) ───────────────────────────────────────────────

async def fetch_instagram_bio(username: str) -> tuple[str, str, str]:
    """
    Anonymously retrieves the bio of an Instagram profile.
    Returns ("Instagram", url, bio_text).
    """
    platform = "Instagram"
    url = f"https://www.instagram.com/{username}/"

    if not INSTALOADER_AVAILABLE:
        logger.warning("instaloader is not installed (pip install instaloader). Step skipped.")
        return platform, url, ""

    logger.info("Requesting Instagram bio for @%s...", username)

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
        # get_running_loop() instead of get_event_loop() (Python 3.10+ compatibility)
        loop = asyncio.get_running_loop()
        bio = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=45,
        )
        logger.info("Instagram bio: %d characters.", len(bio))
        return platform, url, bio

    except asyncio.TimeoutError:
        logger.warning("Instagram: timeout.")
    except instaloader.exceptions.ProfileNotExistsException:
        logger.info("Instagram: profile @%s not found.", username)
    except instaloader.exceptions.LoginRequiredException:
        logger.warning("Instagram: profile is private, login required.")
    except instaloader.exceptions.ConnectionException as exc:
        logger.warning("Instagram: connection error — %s", exc)
    except Exception as exc:
        logger.warning("Instagram: unexpected error — %s", exc)

    return platform, url, ""


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_external_osint(username: str) -> str:
    """
    Runs maigret and instaloader in parallel, saves to external_footprint.

    Returns:
        Summary string for display in Streamlit or CLI.
    """
    logger.info("=== external_osint START: @%s ===", username)

    # return_exceptions=True — failure of one source doesn't stop the other
    maigret_result, instagram_result = await asyncio.gather(
        search_with_maigret(username),
        fetch_instagram_bio(username),
        return_exceptions=True,
    )

    # Process possible exceptions from gather
    maigret_records: list[tuple[str, str, str]] = []
    if isinstance(maigret_result, Exception):
        logger.error("Maigret finished with error: %s", maigret_result)
    else:
        maigret_records = maigret_result

    ig_platform, ig_url, ig_bio = "Instagram", f"https://www.instagram.com/{username}/", ""
    if isinstance(instagram_result, Exception):
        logger.error("Instaloader finished with error: %s", instagram_result)
    else:
        ig_platform, ig_url, ig_bio = instagram_result

    # Save to DB only real findings
    all_records = list(maigret_records)
    if ig_bio:   # save Instagram only if there is a bio (profile actually found)
        all_records.append((ig_platform, ig_url, ig_bio))

    save_footprint_records(username, all_records)

    # ── Summary ──────────────────────────────────────────────────────────────
    count = len(maigret_records)
    if count > 0:
        preview = ", ".join(r[0] for r in maigret_records[:10])
        if count > 10:
            preview += f" and {count - 10} others"
        summary = f"Found {count} profile(s) on external platforms: {preview}."
    else:
        summary = "maigret found no matches on external platforms."

    if ig_bio:
        summary += f"\nInstagram bio (@{username}): {ig_bio[:300]}"
    elif INSTALOADER_AVAILABLE:
        summary += f"\nInstagram (@{username}): profile not found or private."

    logger.info("=== external_osint DONE: @%s ===", username)
    return summary


if __name__ == "__main__":
    import sys
    # logging.basicConfig — only in entry point
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python external_osint.py <username>")
        sys.exit(1)

    target = sys.argv[1].lstrip("@")
    result = asyncio.run(run_external_osint(target))
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)
