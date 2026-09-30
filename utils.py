"""
utils.py — Спільні утиліти для всього конвеєра.

Раніше `_safe_str` дублювався у analyzer_cdp.py та sublimator_cdp.py.
Тепер одна реалізація тут.
"""

import re
import logging
import os

logger = logging.getLogger(__name__)

# Створюємо необхідні директорії
DB_DIR = "databases"
REPORTS_DIR = "reports"
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)


def safe_str(val) -> str:
    """Конвертує списки/None у рядок, безпечний для запису у SQLite."""
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(x) for x in val)
    return str(val)


def sanitize_username(username: str) -> str:
    """
    Видаляє всі символи, що не є безпечними для назви файлу.
    Захист від path-traversal: '../etc/passwd' → 'etcpasswd'.
    """
    clean = re.sub(r'[^a-zA-Z0-9_.\-]', '', username)
    if not clean:
        raise ValueError(f"Некоректний username після санітизації: {username!r}")
    return clean


def db_path_for(username: str) -> str:
    """Повертає стандартний шлях до бази для вказаного профілю."""
    return os.path.join(DB_DIR, f"osint_{sanitize_username(username)}.db")

def report_path_for(username: str, extension: str = "txt") -> str:
    """Повертає стандартний шлях до звіту для вказаного профілю."""
    return os.path.join(REPORTS_DIR, f"profile_{sanitize_username(username)}.{extension}")
