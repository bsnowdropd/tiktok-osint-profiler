"""
retro_profiler.py — Автономний CLI-профайлер для готових БД.

Зміни (рефакторинг):
  - КРИТИЧНИЙ БАГ: порт 11435 → config.OLLAMA_URL (11434)
  - generate_profile обгорнуто у try/except → JSONDecodeError більше не кладе скрипт
  - logging.basicConfig на рівні модуля → перенесено у if __name__ == "__main__"
  - Видалено реальне ім'я користувача з коментаря (рядок 119 оригіналу)
  - Санітизація html_body через markdown.markdown() (захист від HTML injection у PDF)
  - Конфігурація з config.py
"""

import argparse
import base64
import json
import logging
import os
from collections import Counter
from pathlib import Path

import markdown
import pdfkit
import requests

import config

logger = logging.getLogger(__name__)


# ── Утиліти ───────────────────────────────────────────────────────────────────

def process_tags(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    return [tag.strip().lower() for tag in raw_text.split(",") if tag.strip()]


def get_image_base64(image_path: str) -> str:
    p = Path(image_path)
    if p.exists():
        return base64.b64encode(p.read_bytes()).decode("utf-8")
    return ""


def save_as_pdf(text: str, pdf_filename: str, logo_path: str = "logo.png") -> None:
    """Конвертує Markdown у PDF з опціональним логотипом."""
    # markdown.markdown() санітизує LLM-вихід перед передачею в HTML
    html_body = markdown.markdown(text, extensions=["extra"])
    encoded_logo = get_image_base64(logo_path)
    logo_html = (
        f'<img src="data:image/png;base64,{encoded_logo}" style="max-height:60px;margin-bottom:10px;" />'
        if encoded_logo else ""
    )

    html_template = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body {{ font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; margin: 40px; color: #2d3748; }}
    h1, h2, h3 {{ border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-top: 30px; color: #1a202c; }}
    .header {{ text-align:center; margin-bottom:40px; padding-bottom:20px; border-bottom:4px double #cbd5e0; }}
    .header h1 {{ border:none; margin:0; padding:0; }}
    .header p {{ color:#718096; font-size:14px; margin-top:5px; }}
    </style></head><body>
    <div class="header">{logo_html}<h1>ПСИХОЛОГІЧНЕ ДОСЬЄ (OSINT)</h1>
    <p>Ретроспективний аналіз архівної бази даних</p></div>
    {html_body}
    </body></html>
    """
    pdfkit.from_string(html_template, pdf_filename, options={"quiet": ""})


def generate_profile(stats_text: str, total_videos: int) -> str:
    """Запитує Qwen2.5 та повертає Markdown-звіт. Повна обробка помилок."""
    prompt = f"""
<ROLE>
Ти — елітний кримінальний профайлер, психоаналітик та старший OSINT-слідчий.
Склади безжально об'єктивне психологічне досьє на об'єкт розробки.
</ROLE>

<DATA>
Проаналізовано відео: {total_videos}
Частота поведінкових маркерів:
{stats_text}
</DATA>

<RULES>
1. ЖОДНИХ ГАЛЮЦИНАЦІЙ: аналізуй ВИКЛЮЧНО маркери з блоку DATA.
2. АНАЛІТИЧНИЙ ТОН: "Виявлено патерн...", "Статистика вказує на...", "Маркер X свідчить про..."
3. ЗАБОРОНА НА ВОДУ: без вступних фраз; починай одразу по суті.
4. ОЦІНКА ДОСТОВІРНОСТІ: [Висока вірогідність] або [Гіпотеза] біля кожного висновку.
5. ДЕДУКЦІЯ: шукай прихований сенс (ескапізм, вигорання тощо).
</RULES>

<OUTPUT_FORMAT>
(Пиши українською, використовуй Markdown: заголовки, жирний шрифт, списки.)

I. ПСИХОЛОГІЧНИЙ ПОРТРЕТ ТА ВРАЗЛИВОСТІ
- Психотип, темперамент, ймовірний MBTI.
- Домінуючий емоційний стан.
- Головні вразливості та захисні механізми.

II. АНАЛІЗ СТОСУНКІВ ТА RED FLAGS
- Статус, тип прив'язаності, Red/Green Flags.

III. СОЦІАЛЬНО-КАР'ЄРНИЙ ВЕКТОР
- Статус у соціумі, кар'єрні амбіції, стосунки з родиною.

IV. ЛАЙФСТАЙЛ, ТРИГЕРИ ТА ЕСКАПІЗМ
- Способи втечі від реальності, музичний смак як індикатор емоцій, гумор.

V. ОПЕРАТИВНІ РЕКОМЕНДАЦІЇ
- Вектор довіри, тригери відторгнення, стратегія зближення.
</OUTPUT_FORMAT>
    """

    logger.info("Відправка даних до Qwen2.5...")
    try:
        res = requests.post(
            config.OLLAMA_URL,
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_ctx": 32768, "num_predict": 4096, "temperature": 0.4},
            },
            timeout=config.OLLAMA_TIMEOUT_PROFILE,
        )
        res.raise_for_status()
        return res.json().get("response", "") or "Порожня відповідь моделі."
    except requests.exceptions.Timeout:
        logger.error("Тайм-аут при генерації досьє.")
        return "Помилка: тайм-аут запиту до Ollama."
    except requests.exceptions.HTTPError as exc:
        logger.error("HTTP-помилка: %s", exc)
        return f"Помилка HTTP: {exc}"
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Некоректна відповідь від Ollama: %s", exc)
        return "Помилка: не вдалося розпарсити відповідь моделі."
    except Exception as exc:
        logger.error("Непередбачена помилка: %s", exc, exc_info=True)
        return f"Помилка: {exc}"


# ── Головна функція ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Генерація досьє з готової БД")
    parser.add_argument("db_file", help="Шлях до файлу osint_<username>.db")
    args = parser.parse_args()

    if not os.path.exists(args.db_file):
        logger.error("Файл %s не знайдено!", args.db_file)
        return

    import sqlite3
    conn = sqlite3.connect(args.db_file)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT interests, hobbies, relations, music_taste, raw_result FROM analysis"
        ).fetchall()
    except sqlite3.OperationalError:
        logger.error("Таблиця 'analysis' відсутня. Спочатку виконайте сублімацію тегів.")
        conn.close()
        return
    finally:
        conn.close()

    if not rows:
        logger.warning("Немає проаналізованих відео в цій базі.")
        return

    stats: dict[str, Counter] = {
        "Інтереси":  Counter(),
        "Хобі":      Counter(),
        "Стосунки":  Counter(),
        "Музика":    Counter(),
        "Категорії": Counter(),
    }
    for r in rows:
        stats["Інтереси"].update(process_tags(r["interests"]))
        stats["Хобі"].update(process_tags(r["hobbies"]))
        stats["Стосунки"].update(process_tags(r["relations"]))
        stats["Музика"].update(process_tags(r["music_taste"]))
        stats["Категорії"].update(process_tags(r["raw_result"]))

    filtered = {k: dict(v.most_common(40)) for k, v in stats.items() if v}
    stats_text = json.dumps(filtered, ensure_ascii=False, indent=2)

    report = generate_profile(stats_text, len(rows))

    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(args.db_file))[0]
    txt_file = os.path.join(reports_dir, f"profile_{base_name}.txt")
    pdf_file = os.path.join(reports_dir, f"profile_{base_name}.pdf")

    with open(txt_file, "w", encoding="utf-8") as fh:
        fh.write(report)

    try:
        save_as_pdf(report, pdf_file)
        logger.info("[✓] PDF: %s", pdf_file)
    except Exception as exc:
        logger.error("PDF не створено: %s", exc)

    logger.info("[✓] Досьє готово!")
    logger.info("    📄 TXT: %s", txt_file)


if __name__ == "__main__":
    # logging.basicConfig — тільки в точці входу, не на рівні модуля
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    main()