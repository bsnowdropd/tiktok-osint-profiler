"""
reset_db.py — Скидання результатів аналізу для повторної обробки.

Зміни (рефакторинг):
  - argparse: тепер приймає --db (шлях до бази) замість hardcoded "tiktok_reposts.db"
  - Флаг --confirm для захисту від випадкового запуску (деструктивна операція)
  - logging замість print
  - Коректне виведення: спочатку DELETE, потім UPDATE — rowcount від DELETE
"""

import argparse
import logging
import sqlite3
import sys

from utils import db_path_for, sanitize_username

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def reset_analysis(db_path: str, confirm: bool = False) -> None:
    """
    Видаляє всі записи з `analysis` та скидає `reposts.analyzed → 0`.
    Дозволяє повторно запустити кроки 2–6 без повторного парсингу.

    Args:
        db_path:  Шлях до osint_<username>.db.
        confirm:  Якщо False — виводить попередження і виходить без змін.
    """
    if not confirm:
        logger.warning(
            "⚠️  ДЕСТРУКТИВНА ОПЕРАЦІЯ: видалить усі результати аналізу у %s!", db_path
        )
        logger.warning("Додайте прапорець --confirm для підтвердження.")
        sys.exit(0)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM analysis")
        deleted = cursor.rowcount   # зберігаємо до наступного execute

        cursor.execute("UPDATE reposts SET analyzed = 0")
        updated = cursor.rowcount

        conn.commit()
        conn.close()

        logger.info("[✓] analysis: видалено %d записів.", deleted)
        logger.info("[✓] reposts:  скинуто %d записів (analyzed→0).", updated)

    except sqlite3.OperationalError as exc:
        logger.error("Помилка БД: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Скидання результатів аналізу (analysis) у базі OSINT."
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--user",
        help="Нікнейм TikTok (скрипт сам знайде osint_<user>.db)",
    )
    group.add_argument(
        "--db",
        help="Прямий шлях до .db файлу (наприклад: osint_durov.db)",
    )

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Підтвердити деструктивну операцію",
    )

    args = parser.parse_args()

    if args.user:
        try:
            target_db = db_path_for(args.user)
        except ValueError as exc:
            logger.error("Некоректний username: %s", exc)
            sys.exit(1)
    else:
        target_db = args.db

    reset_analysis(target_db, confirm=args.confirm)