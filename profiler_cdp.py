"""
profiler_cdp.py — Psychological profile generator (step 6).

Changes (refactoring):
  - Removed dead variable `optimized_data` (declared but not used)
  - Configuration from config.py (OLLAMA_URL, MODEL, OLLAMA_TIMEOUT_PROFILE)
  - db_schema.get_connection() instead of direct sqlite3.connect()
  - HTML-sanitization of LLM response body before passing to pdfkit
    (protection against HTML injection in PDF)
  - generate_profile now takes temperature as a parameter
    (for future integration with Live Prompt Engineering in Streamlit)
  - Removed raw_result in process_tags: raw_result == summary (1-2 words),
    now it's "Video categories (essence)" without additional processing
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
    """Splits a string of tags into a list of clean words in lowercase."""
    if not raw_text:
        return []
        
    return [tag.strip().lower() for tag in raw_text.split(",") if tag.strip()]


def generate_profile(
    aggregated_stats: dict,
    total_videos: int,
    temperature: float = 0.3,
) -> str:
    """
    Sends aggregated tag statistics to Qwen2.5 and returns a Markdown report.

    Args:
        aggregated_stats: dictionary {category: {tag: count}}
        total_videos:     total number of videos in analysis
        temperature:      generation temperature (0.1-0.7); controlled from Streamlit sidebar
    """
    stats_text = json.dumps(aggregated_stats, ensure_ascii=False, indent=2)

    prompt = f"""
    <ROLE>
    You are an elite OSINT profiler and analyst. Your task: create a psychological profile
    based on TikTok tags. Write EXCLUSIVELY in natural English.
    </ROLE>

    <DATA>
    Total videos: {total_videos}
    Tag statistics:
    {stats_text}
    </DATA>

    <CRITICAL_RULES>
    1. MBTI LOGIC: "I" = introvert, "E" = extrovert.
    2. TIKTOK CONTEXT: tags "weapon"/"police" near games - this is esports or black humor.
    3. NO SLANG: "friend interactions" -> "social contacts".
    4. NO TAUTOLOGY: each point is a unique thought.
    5. FORMATTING: short, professional, without "fluff".
    </CRITICAL_RULES>

    <TEMPLATE>
    ### I. PSYCHOLOGICAL PORTRAIT AND COGNITIVE BASE
    - **Psychotype (MBTI) and temperament:** [type + 1 sentence explanation]
    - **Basic values and emotional background:** [motivators + usual emotional state]
    - **Inner conflicts:** [anxieties behind humor/games/escapism]

    ### II. SOCIAL DYNAMICS AND RELATIONSHIPS
    - **Current vector:** [new acquaintances vs. narrow circle/loneliness]
    - **Attachment style:** [secure / avoidant / anxious + argument]
    - **Communication triggers:** [behavior that causes discomfort]

    ### III. LIFESTYLE, AESTHETICS AND ESCAPISM
    - **Dopamine release:** [main ways to relax]
    - **Humor specifics:** [black / ironic / absurd and why]
    - **Intellectual requests:** [topics for reflection and analysis]

    ### IV. OPERATIONAL VECTOR (COMMUNICATION STRATEGY)
    - **Trust triggers:** [2-3 topics for quick contact establishment]
    - **Rejection triggers:** [what to strictly avoid]
    - **Reaction to criticism:** [aggression / ignoring / humor]
    </TEMPLATE>
    """

    logger.info(
        "AI is analyzing statistics (%d characters). Temperature: %.1f...",
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
        logger.error("AI server error: HTTP %d", response.status_code)
        return f"Server error: {response.status_code}"
    except requests.exceptions.Timeout:
        logger.error("AI did not finish in the allotted time.")
        return "Error: timeout. Try a more powerful GPU or reduce the data volume."
    except Exception as exc:
        logger.error("Connection error: %s", exc, exc_info=True)
        return f"Connection error: {exc}"


def save_as_pdf(text: str, txt_filename: str) -> str:
    """
    Converts Markdown report to PDF.

    HTML body from LLM is sanitized via markdown.markdown(),
    which prevents HTML injection in the final PDF.
    """
    pdf_filename = txt_filename.replace(".txt", ".pdf")

    # markdown.markdown() escapes dangerous HTML in the input text
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
            <h1>PSYCHOLOGICAL DOSSIER (OSINT)</h1>
            <p>Generated automatically based on TikTok metadata analysis</p>
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
        logger.error("Database file %s not found.", db_path)
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
        logger.warning("No data for profiling (no videos analyzed).")
        return

    logger.info("Aggregating tags from %d videos...", len(rows))

    stats: dict[str, Counter] = {
        "Interests": Counter(),
        "Hobbies": Counter(),
        "Relationships": Counter(),
        "Music taste": Counter(),
        "Video categories (essence)": Counter(),
    }

    for row in rows:
        stats["Interests"].update(process_tags(row["interests"]))
        stats["Hobbies"].update(process_tags(row["hobbies"]))
        stats["Relationships"].update(process_tags(row["relations"]))
        stats["Music taste"].update(process_tags(row["music_taste"]))
        stats["Video categories (essence)"].update(process_tags(row["raw_result"]))

    # Top 40 in each category
    filtered_stats = {
        cat: {k: v for k, v in counter.most_common(40) if k}
        for cat, counter in stats.items()
        if counter
    }

    final_report = generate_profile(filtered_stats, len(rows), temperature=temperature)

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(final_report)
    logger.info("Text report: %s", output_file)

    try:
        pdf_name = save_as_pdf(final_report, output_file)
        logger.info("PDF report: %s", pdf_name)
    except Exception as exc:
        logger.error(
            "Failed to create PDF (check pdfkit and wkhtmltopdf): %s", exc
        )

    logger.info("Psychological portrait is ready!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")
    main()