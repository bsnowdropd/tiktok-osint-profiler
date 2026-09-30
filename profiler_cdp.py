"""
profiler_cdp.py — Генератор психологічного досьє (крок 6).

Зміни (рефакторинг):
  - Видалено мертву змінну `optimized_data` (оголошувалась, але не використовувалась)
  - Конфігурація з config.py (OLLAMA_URL, MODEL, OLLAMA_TIMEOUT_PROFILE)
  - db_schema.get_connection() замість прямого sqlite3.connect()
  - HTML-санітизація тіла LLM-відповіді перед передачею у pdfkit
    (захист від HTML injection у PDF)
  - generate_profile тепер приймає temperature як параметр
    (для майбутньої інтеграції з Live Prompt Engineering у Streamlit)
  - Прибрано raw_result у process_tags: raw_result == summary (1–2 слова),
    тепер це "Категорії відео (суть)" без додаткової обробки
"""

import json
import logging
import os
from collections import Counter

import markdown
import pdfkit
import requests

import config
from db_schema import get_connection

logger = logging.getLogger(__name__)


def process_tags(raw_text: str) -> list[str]:
    """Розбиває рядок з тегами на список чистих слів у нижньому регістрі."""
    if not raw_text:
        return []
    return [tag.strip().lower() for tag in raw_text.split(",") if tag.strip()]


def generate_profile(
    aggregated_stats: dict,
    total_videos: int,
    temperature: float = 0.3,
) -> str:
    """
    Надсилає агреговану статистику тегів до Qwen2.5 і повертає Markdown-звіт.

    Args:
        aggregated_stats: словник {категорія: {тег: кількість}}
        total_videos:     загальна кількість відео в аналізі
        temperature:      температура генерації (0.1–0.7); керується з Streamlit sidebar
    """
    stats_text = json.dumps(aggregated_stats, ensure_ascii=False, indent=2)

    prompt = f"""
    <ROLE>
    Ти — елітний OSINT-профайлер та аналітик. Твоє завдання: скласти психологічне досьє
    на основі тегів з TikTok. Пиши ВИКЛЮЧНО природною українською мовою.
    </ROLE>

    <DATA>
    Всього відео: {total_videos}
    Статистика тегів:
    {stats_text}
    </DATA>

    <CRITICAL_RULES>
    1. ЛОГІКА MBTI: "I" = інтроверт, "E" = екстраверт.
    2. TIKTOK-КОНТЕКСТ: теги "зброя"/"поліція" поруч з іграми — це кіберспорт або чорний гумор.
    3. ЗАБОРОНА СУРЖИКУ: "дружбені взаємодійства" → "соціальні контакти".
    4. ЗАБОРОНА ТАВТОЛОГІЇ: кожен пункт — унікальна думка.
    5. ФОРМАТУВАННЯ: коротко, професійно, без "води".
    </CRITICAL_RULES>

    <TEMPLATE>
    ### I. ПСИХОЛОГІЧНИЙ ПОРТРЕТ ТА КОГНІТИВНА БАЗА
    - **Психотип (MBTI) та темперамент:** [тип + 1 речення пояснення]
    - **Базові цінності та емоційний фон:** [мотиватори + звичний емоційний стан]
    - **Внутрішні конфлікти:** [тривоги за гумором/іграми/ескапізмом]

    ### II. СОЦІАЛЬНА ДИНАМІКА ТА СТОСУНКИ
    - **Поточний вектор:** [нові знайомства vs. вузьке коло/самотність]
    - **Тип прив'язаності:** [надійний / уникливий / тривожний + аргумент]
    - **Тригери у спілкуванні:** [поведінка, що викликає дискомфорт]

    ### III. ЛАЙФСТАЙЛ, ЕСТЕТИКА ТА ЕСКАПІЗМ
    - **Дофамінова розрядка:** [основні способи відпочинку]
    - **Специфіка гумору:** [чорний / іронічний / абсурдний і навіщо]
    - **Інтелектуальні запити:** [теми для роздумів та аналізу]

    ### IV. ОПЕРАТИВНИЙ ВЕКТОР (СТРАТЕГІЯ КОМУНІКАЦІЇ)
    - **Тригери довіри:** [2–3 теми для швидкого встановлення контакту]
    - **Тригери відторгнення:** [чого категорично уникати]
    - **Реакція на критику:** [агресія / ігнорування / гумор]
    </TEMPLATE>
    """

    logger.info(
        "ШІ аналізує статистику (%d символів). Температура: %.1f...",
        len(stats_text),
        temperature,
    )

    try:
        response = requests.post(
            config.OLLAMA_URL,
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_ctx": 32768,
                    "num_predict": 4096,
                    "temperature": temperature,
                },
            },
            timeout=config.OLLAMA_TIMEOUT_PROFILE,
        )
        if response.status_code == 200:
            return response.json().get("response", "")
        logger.error("Помилка сервера ШІ: HTTP %d", response.status_code)
        return f"Помилка сервера: {response.status_code}"
    except requests.exceptions.Timeout:
        logger.error("ШІ не вклався у відведений час.")
        return "Помилка: тайм-аут. Спробуйте потужнішу GPU або зменшіть обсяг даних."
    except Exception as exc:
        logger.error("Помилка з'єднання: %s", exc, exc_info=True)
        return f"Помилка з'єднання: {exc}"


def save_as_pdf(text: str, txt_filename: str) -> str:
    """
    Конвертує Markdown-звіт у PDF.

    HTML-тіло від LLM санітизується через markdown.markdown(),
    що уникне HTML injection у кінцевому PDF.
    """
    pdf_filename = txt_filename.replace(".txt", ".pdf")

    # markdown.markdown() ескейпить небезпечний HTML у вхідному тексті
    html_body = markdown.markdown(text, extensions=["extra"])

    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                line-height: 1.6;
                color: #2d3748;
                margin: 40px;
            }}
            h1, h2, h3 {{
                color: #1a202c;
                border-bottom: 2px solid #edf2f7;
                padding-bottom: 8px;
                margin-top: 30px;
            }}
            p {{ text-align: justify; }}
            ul {{ margin-bottom: 20px; }}
            strong {{ color: #000; font-weight: 700; }}
            .header {{
                text-align: center;
                margin-bottom: 40px;
                padding-bottom: 20px;
                border-bottom: 4px double #cbd5e0;
            }}
            .header h1 {{ border: none; margin: 0; padding: 0; }}
            .header p {{ color: #718096; font-size: 14px; text-align: center; margin-top: 5px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>ПСИХОЛОГІЧНЕ ДОСЬЄ (OSINT)</h1>
            <p>Згенеровано автоматично на основі аналізу метаданих TikTok</p>
        </div>
        {html_body}
    </body>
    </html>
    """

    options = {"quiet": "", "encoding": "UTF-8"}
    pdfkit.from_string(html_template, pdf_filename, options=options)
    return pdf_filename


def main(
    db_path: str = "osint_unknown.db",
    output_file: str = "profile_unknown.txt",
    temperature: float = 0.3,
) -> None:
    if not os.path.exists(db_path):
        logger.error("Файл бази %s не знайдено.", db_path)
        return

    conn = get_connection(db_path)

    rows = conn.execute(
        """
        SELECT interests, hobbies, relations, music_taste, raw_result
        FROM analysis
        WHERE interests IS NOT NULL OR raw_result IS NOT NULL
        """
    ).fetchall()

    conn.close()

    if not rows:
        logger.warning("Немає даних для профайлінгу (жодне відео не проаналізоване).")
        return

    logger.info("Агрегуємо теги з %d відео...", len(rows))

    stats: dict[str, Counter] = {
        "Інтереси":              Counter(),
        "Хобі":                  Counter(),
        "Стосунки":              Counter(),
        "Музичний смак":         Counter(),
        "Категорії відео (суть)": Counter(),
    }

    for row in rows:
        stats["Інтереси"].update(process_tags(row["interests"]))
        stats["Хобі"].update(process_tags(row["hobbies"]))
        stats["Стосунки"].update(process_tags(row["relations"]))
        stats["Музичний смак"].update(process_tags(row["music_taste"]))
        stats["Категорії відео (суть)"].update(process_tags(row["raw_result"]))

    # Топ-40 у кожній категорії
    filtered_stats = {
        cat: {k: v for k, v in counter.most_common(40) if k}
        for cat, counter in stats.items()
        if counter
    }

    final_report = generate_profile(filtered_stats, len(rows), temperature=temperature)

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(final_report)
    logger.info("Текстовий звіт: %s", output_file)

    try:
        pdf_name = save_as_pdf(final_report, output_file)
        logger.info("PDF-звіт: %s", pdf_name)
    except Exception as exc:
        logger.error(
            "Не вдалося створити PDF (перевірте pdfkit та wkhtmltopdf): %s", exc
        )

    logger.info("Психологічний портрет готовий!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main()