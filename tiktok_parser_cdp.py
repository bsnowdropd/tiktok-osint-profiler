"""
tiktok_parser_cdp.py — Парсер TikTok-репостів у повноекранному режимі.

Зміни (рефакторинг):
  - КРИТИЧНИЙ БАГ: `mentions: str = ""` перенесено в кінець dataclass Repost
    (Python забороняє поле з дефолтом перед обов'язковими — TypeError при кожному викл.)
  - INSERT OR REPLACE → ON CONFLICT DO UPDATE що НЕ скидає analyzed=1
  - pickle → JSON для збереження/завантаження cookies (безпека)
  - VIDEO_DIR.mkdir() прибрано з рівня модуля → викликається всередині функції
  - Jitter до SCROLL_PAUSE (антибот)
  - init_db замінено на db_schema.get_connection()
  - Імпорт config для єдиних констант
"""

import time
import json
import os
import re
import random
import sqlite3
import logging
import requests
from pathlib import Path
from dataclasses import dataclass, field

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

import config
from db_schema import get_connection

logger = logging.getLogger(__name__)

# ── Конфігурація ──────────────────────────────────────────────────────────────
COOKIES_FILE = Path(config.COOKIES_FILE)   # тепер JSON, не pickle
VIDEO_DIR    = Path(config.VIDEO_DIR)


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class Repost:
    """
    DTO метаданих одного TikTok-репосту.

    Поля БЕЗ дефолту (обов'язкові) йдуть ПЕРШИМИ.
    Поля З дефолтом — в кінці (вимога Python dataclasses).
    """
    video_id:   str
    author:     str
    author_url: str
    description: str
    hashtags:   str      # JSON-рядок зі списком хештегів
    sound:      str
    likes:      str
    comments:   str
    shares:     str
    url:        str
    # ── поля з дефолтом (завжди в кінці) ─────────────────────────────────────
    local_path: str = ""
    mentions:   str = ""   # JSON-рядок зі списком @-згадок


# ── DB ────────────────────────────────────────────────────────────────────────

def save_repost(conn: sqlite3.Connection, r: Repost) -> bool:
    """
    Зберігає Repost у БД.
    ON CONFLICT DO UPDATE зберігає значення `analyzed` — відео не буде
    повторно оброблене LLM при наступному запуску парсера.
    """
    try:
        conn.execute(
            """
            INSERT INTO reposts
                (video_id, author, author_url, description, hashtags, mentions,
                 sound, likes, comments, shares, url, local_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(video_id) DO UPDATE SET
                local_path  = excluded.local_path,
                description = excluded.description,
                hashtags    = excluded.hashtags,
                mentions    = excluded.mentions,
                parsed_at   = CURRENT_TIMESTAMP
                -- analyzed НЕ оновлюємо: вже оброблені відео не перепрацьовуються
            """,
            (
                r.video_id, r.author, r.author_url, r.description,
                r.hashtags, r.mentions, r.sound, r.likes, r.comments,
                r.shares, r.url, r.local_path,
            ),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Помилка збереження %s у БД: %s", r.video_id, exc)
        return False


# ── Chrome Driver ──────────────────────────────────────────────────────────────

def _create_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--mute-audio")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# ── Cookies (JSON замість pickle) ─────────────────────────────────────────────

def _save_cookies(driver: webdriver.Chrome) -> None:
    """Зберігає cookies у JSON (замість небезпечного pickle)."""
    cookies = driver.get_cookies()
    COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    logger.info("Cookies збережено → %s", COOKIES_FILE)


def _load_cookies(driver: webdriver.Chrome) -> bool:
    """Завантажує cookies з JSON-файлу."""
    if not COOKIES_FILE.exists():
        return False
    driver.get("https://www.tiktok.com")
    time.sleep(5)
    try:
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Не вдалося прочитати cookies: %s", exc)
        return False

    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass
    logger.info("Cookies завантажено з %s", COOKIES_FILE)
    return True


def _wait_for_manual_login(driver: webdriver.Chrome) -> None:
    driver.get("https://www.tiktok.com/login")
    logger.warning("=" * 50)
    logger.warning("ПОТРІБЕН ЛОГІН! Залогінься вручну у відкритому браузері.")
    logger.warning("=" * 50)
    WebDriverWait(driver, 300).until(lambda d: "login" not in d.current_url)
    time.sleep(3)
    logger.info("Успішний вхід!")


def _ensure_authenticated(driver: webdriver.Chrome) -> None:
    if _load_cookies(driver):
        driver.refresh()
        time.sleep(4)
        if "login" not in driver.current_url:
            logger.info("Авторизація через cookies успішна.")
            return
        logger.warning("Cookies застаріли, потрібен повторний логін.")

    _wait_for_manual_login(driver)
    _save_cookies(driver)


# ── Завантаження через TikWM API ──────────────────────────────────────────────

def download_video_tikwm(video_id: str) -> str | None:
    """Завантажує відео/фото-карусель через TikWM API."""
    VIDEO_DIR.mkdir(exist_ok=True)   # створюємо тільки при реальному використанні
    output_path = VIDEO_DIR / f"{video_id}.mp4"

    api_url = f"https://www.tikwm.com/api/?url=https://www.tiktok.com/video/{video_id}"
    try:
        res = requests.get(api_url, timeout=20)
        res.raise_for_status()
        data = res.json()
        if data.get("code") == 0:
            video_data = data["data"]
            
            # Якщо це фото-карусель (немає play_url, але є images)
            if "images" in video_data and not video_data.get("play"):
                logger.warning("Відео %s є фотокаруселлю, пропускаємо збереження mp4.", video_id)
                return None

            play_url = video_data.get("play")
            if play_url:
                vid_res = requests.get(play_url, stream=True, timeout=30)
                vid_res.raise_for_status()
                with open(output_path, "wb") as fh:
                    for chunk in vid_res.iter_content(chunk_size=8192):
                        fh.write(chunk)
                
                # Валідація розміру (якщо пустий або < 1KB)
                if os.path.exists(output_path) and os.path.getsize(output_path) < 1024:
                    os.remove(output_path)
                    logger.warning("Завантажений файл %s надто малий (битий), видалено.", video_id)
                    return None
                    
                return str(output_path)
        else:
            logger.debug("TikWM API: %s", data.get("msg"))
    except Exception as exc:
        logger.error("Помилка TikWM для %s: %s", video_id, exc)
    return None


# ── Головна логіка ────────────────────────────────────────────────────────────

def parse_fullscreen_feed(
    username: str,
    db_path: str = "osint_unknown.db",
    limit: int = 100,
) -> list[dict]:
    """Гортає стрічку репостів TikTok і зберігає метадані у БД."""
    conn = get_connection(db_path)
    driver = _create_driver()
    results: list[dict] = []

    try:
        _ensure_authenticated(driver)

        profile_url = f"https://www.tiktok.com/@{username}?tab=repost"
        logger.info("Відкриваємо: %s", profile_url)
        driver.get(profile_url)
        time.sleep(3)

        # ── Клік на вкладку "Репости" ─────────────────────────────────────────
        tab_selectors = [
            '[data-e2e="repost-tab"]',
            '//span[contains(text(),"Repost") or contains(text(),"Репост")]',
            'svg path[d*="M19.043 4.296"]',
        ]
        clicked = False
        for sel in tab_selectors:
            try:
                el = (
                    driver.find_element(By.XPATH, sel)
                    if sel.startswith("//")
                    else driver.find_element(By.CSS_SELECTOR, sel)
                )
                driver.execute_script("arguments[0].click();", el)
                time.sleep(3)
                logger.info("[✓] Вкладку 'Репости' відкрито.")
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            logger.warning("Не вдалося знайти вкладку репостів.")
            return []

        time.sleep(5)

        # ── Клік на перше відео ───────────────────────────────────────────────
        video_selectors = [
            '[data-e2e="user-post-item"] a',
            'div[class*="DivItemContainerV2"] a',
            'a[href*="/video/"]',
            'a[href*="/photo/"]',
        ]
        opened = False
        for sel in video_selectors:
            try:
                for vid in driver.find_elements(By.CSS_SELECTOR, sel):
                    href = vid.get_attribute("href") or ""
                    if "/video/" in href or "/photo/" in href:
                        driver.execute_script("arguments[0].click();", vid)
                        time.sleep(4)
                        logger.info("[✓] Перше відео відкрито.")
                        opened = True
                        break
                if opened:
                    break
            except Exception:
                continue

        if not opened:
            logger.error("Не вдалося відкрити перше відео.")
            return []

        logger.info("Починаємо обробку (макс. %d постів)...", limit)
        seen_ids: set[str] = set()

        for i in range(limit):
            # Jitter-пауза (антибот): випадкова затримка у заданому діапазоні
            pause = random.uniform(config.SCROLL_PAUSE_MIN, config.SCROLL_PAUSE_MAX)
            time.sleep(pause)

            current_url = driver.current_url
            clean_url = current_url.split("?")[0]
            video_id = clean_url.rstrip("/").split("/")[-1]

            # Зупинка при дублікаті або нечисловому ID
            if video_id in seen_ids:
                logger.info("Кінець стрічки (дублікат ID %s).", video_id)
                break
            if not video_id.isdigit():
                logger.info("Нечисловий URL-сегмент (%s) — можлива зміна сторінки, продовжуємо.", video_id)
                webdriver.ActionChains(driver).send_keys(Keys.ARROW_DOWN).perform()
                continue

            seen_ids.add(video_id)
            logger.info("[%d/%d] Обробка: %s", i + 1, limit, video_id)

            # ── Витягуємо метадані ────────────────────────────────────────────
            author = "unknown"
            try:
                author = driver.find_element(
                    By.CSS_SELECTOR, '[data-e2e="browser-nickname"]'
                ).text.strip()
            except Exception:
                pass

            desc, hashtags, mentions = "", [], []
            try:
                desc_el = driver.find_element(
                    By.CSS_SELECTOR, '[data-e2e="browse-video-desc"]'
                )
                desc = desc_el.text.strip()
                hashtags = [w for w in desc.split() if w.startswith("#")]
                mentions = re.findall(r"@([\w.\-]+)", desc)
            except Exception:
                pass

            # ── Завантаження відео ────────────────────────────────────────────
            local_path = download_video_tikwm(video_id)
            if local_path:
                logger.info("  [✓] Збережено: %s", local_path)
            else:
                logger.warning("  [!] Не вдалося завантажити відео.")

            # ── Зберігаємо у БД ───────────────────────────────────────────────
            r = Repost(
                video_id=video_id,
                author=author,
                author_url=f"https://www.tiktok.com/@{author}",
                description=desc,
                hashtags=json.dumps(hashtags, ensure_ascii=False),
                mentions=json.dumps(mentions, ensure_ascii=False),
                sound="", likes="", comments="", shares="",
                url=clean_url,
                local_path=local_path or "",
            )
            if save_repost(conn, r):
                results.append(r.__dict__)

            webdriver.ActionChains(driver).send_keys(Keys.ARROW_DOWN).perform()

        logger.info("Парсинг завершено. Нових записів: %d", len(results))
        return results

    except KeyboardInterrupt:
        logger.warning("Перервано користувачем.")
        return results
    except Exception as exc:
        logger.error("Неочікувана помилка: %s", exc, exc_info=True)
        return results
    finally:
        driver.quit()
        conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="TikTok Парсер (CDP)")
    ap.add_argument("--user",  required=True, help="Нікнейм TikTok")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    from utils import db_path_for
    parse_fullscreen_feed(args.user, db_path=db_path_for(args.user), limit=args.limit)