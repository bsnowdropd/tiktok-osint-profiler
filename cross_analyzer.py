"""
cross_analyzer.py — Крос-аналіз спільних репостів між усіма OSINT-профілями.

Алгоритм
────────
1. Сканує директорію на osint_*.db файли.
2. Через DatabaseManager зчитує (video_id, url, author) кожного профілю —
   з'єднання відразу закривається після читання (→ no "database is locked").
3. Будує mapping: video_id → {username_1: url, username_2: url, ...}
4. Залишає лише ті video_id, де ≥ min_users профілів.
5. Повертає CrossAnalysisResult з трьома форматами даних:
     .shared          list[SharedVideo]            — для карток UI
     .overlaps        dict[(user_a, user_b), int]  — для ребер PyVis
     .to_pairs_df()   DataFrame Target_1/Target_2/Shared_Video_ID/Video_URL
     .to_maltego_df() DataFrame Source/Target/Edge_Type/Weight  (для Maltego)

Використання
────────────
    from cross_analyzer import find_shared_reposts

    result = find_shared_reposts(project_dir="/abs/path")

    # Для AgGrid / st.dataframe
    df_pairs   = result.to_pairs_df()
    df_maltego = result.to_maltego_df()

    # Для PyVis-графа
    for (u_a, u_b), count in result.overlaps.items():
        G.add_edge(u_a, u_b, weight=count, label="Спільний контент")
"""

from __future__ import annotations

import glob
import io
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import pandas as pd

from db_manager import DatabaseManager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Моделі даних
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SharedVideo:
    """Відео, яке репостнули двоє або більше цілей."""
    video_id: str
    url:      str
    users:    list[str]   # нікнейми цілей у алфавітному порядку

    @property
    def user_count(self) -> int:
        return len(self.users)

    @property
    def users_display(self) -> str:
        return " · ".join(f"@{u}" for u in self.users)


@dataclass
class CrossAnalysisResult:
    """
    Результат крос-аналізу всіх баз даних.

    Attributes
    ----------
    shared   : list[SharedVideo]
    overlaps : dict[tuple[str, str], int]
        Пари (user_a, user_b) → кількість спільних відео.
        Ключ завжди відсортований алфавітно: user_a < user_b.
    stats    : dict
        Загальна статистика сканування.
    """
    shared:   list[SharedVideo]          = field(default_factory=list)
    overlaps: dict[tuple[str, str], int] = field(default_factory=dict)
    stats:    dict                        = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return len(self.shared) == 0

    def users_with_overlap(self) -> set[str]:
        """Всі нікнейми, що мають хоча б один спільний репост."""
        out: set[str] = set()
        for u_a, u_b in self.overlaps:
            out.add(u_a)
            out.add(u_b)
        return out

    # ── DataFrame-перетворювачі ────────────────────────────────────────────────

    def to_pairs_df(self) -> pd.DataFrame:
        """
        DataFrame для AgGrid: кожен рядок — одна пара цілей + відео.

        Columns
        -------
        Target_1      : str   — нікнейм першої цілі
        Target_2      : str   — нікнейм другої цілі
        Shared_Video_ID : str — TikTok video_id
        Video_URL     : str   — посилання на відео

        Примітка: якщо відео репостнули N>2 цілей, воно з'явиться у C(N,2)
        рядках (для кожної пари). Це дозволяє фільтрувати по Target_1/Target_2.
        """
        if self.is_empty:
            return pd.DataFrame(
                columns=["Target_1", "Target_2", "Shared_Video_ID", "Video_URL"]
            )
        rows = []
        for sv in self.shared:
            for u_a, u_b in combinations(sv.users, 2):
                key = (min(u_a, u_b), max(u_a, u_b))
                rows.append({
                    "Target_1":        key[0],
                    "Target_2":        key[1],
                    "Shared_Video_ID": sv.video_id,
                    "Video_URL":       sv.url,
                })
        df = pd.DataFrame(rows)
        # Сортуємо: спочатку найактивніші пари
        pair_counts = df.groupby(["Target_1", "Target_2"]).size().rename("_cnt")
        df = df.merge(pair_counts, on=["Target_1", "Target_2"])
        df = df.sort_values(["_cnt", "Target_1", "Target_2"], ascending=[False, True, True])
        df = df.drop(columns=["_cnt"]).reset_index(drop=True)
        return df

    def to_maltego_df(self) -> pd.DataFrame:
        """
        DataFrame у форматі Maltego CSV-імпорту.

        Columns
        -------
        Source     : str   — нікнейм 1 (ціль)
        Target     : str   — нікнейм 2 (ціль)
        Edge_Type  : str   — завжди "Shared_Repost"
        Weight     : int   — кількість спільних відео між парою

        Використання у Maltego
        ──────────────────────
        Import → Import from CSV → Map columns:
          Source    → Entity 1 (Person / Social Media)
          Target    → Entity 2
          Edge_Type → Link Label
          Weight    → Link Thickness (або Bookmark)
        """
        if not self.overlaps:
            return pd.DataFrame(
                columns=["Source", "Target", "Edge_Type", "Weight"]
            )
        rows = [
            {
                "Source":    u_a,
                "Target":    u_b,
                "Edge_Type": "Shared_Repost",
                "Weight":    count,
            }
            for (u_a, u_b), count in sorted(
                self.overlaps.items(), key=lambda x: x[1], reverse=True
            )
        ]
        return pd.DataFrame(rows)

    def to_maltego_csv_bytes(self) -> bytes:
        """Повертає CSV як bytes для st.download_button."""
        df = self.to_maltego_df()
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8")
        return buf.getvalue().encode("utf-8")

    def to_pairs_csv_bytes(self) -> bytes:
        """Повертає повний pairs-CSV як bytes для st.download_button."""
        df = self.to_pairs_df()
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8")
        return buf.getvalue().encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Головна функція
# ══════════════════════════════════════════════════════════════════════════════

def find_shared_reposts(
    project_dir: str | None = None,
    min_users: int = 2,
) -> CrossAnalysisResult:
    """
    Знаходить відео, які репостнули ≥ `min_users` різних цілей (профілів).

    Parameters
    ----------
    project_dir : str | None
        Директорія пошуку osint_*.db. Якщо None — поточна робоча директорія.
    min_users : int
        Мінімальна кількість профілів для вважання відео «спільним».

    Returns
    -------
    CrossAnalysisResult
    """
    base_dir = project_dir or os.getcwd()
    pattern  = os.path.join(base_dir, "databases", "osint_*.db")
    db_files = sorted(glob.glob(pattern))

    if not db_files:
        logger.warning("cross_analyzer: баз osint_*.db не знайдено у %s", base_dir)
        return CrossAnalysisResult(stats={"error": "Баз даних не знайдено"})

    logger.info("cross_analyzer: сканую %d баз…", len(db_files))

    # ── Крок 1: video_id → {username: url} ───────────────────────────────────
    video_to_users: dict[str, dict[str, str]] = defaultdict(dict)
    scanned_users: list[str] = []
    total_reposts  = 0
    errors: list[str] = []

    for db_file in db_files:
        username = _username_from_path(db_file)
        mgr = DatabaseManager(db_file)

        try:
            # З'єднання відкривається і закривається всередині методу
            triples = mgr.get_video_ids_with_author()
        except Exception as exc:
            msg = f"Помилка читання {os.path.basename(db_file)}: {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

        scanned_users.append(username)
        total_reposts += len(triples)

        for video_id, url, _author in triples:
            if video_id:
                # Ключ мапи — нікнейм цілі (не автора відео)
                video_to_users[video_id][username] = url

    # ── Крок 2: фільтрація ≥ min_users ───────────────────────────────────────
    shared: list[SharedVideo] = []
    for video_id, user_url_map in video_to_users.items():
        if len(user_url_map) >= min_users:
            sorted_users = sorted(user_url_map.keys())
            url = user_url_map[sorted_users[0]] or ""
            shared.append(SharedVideo(
                video_id=video_id,
                url=url,
                users=sorted_users,
            ))
    shared.sort(key=lambda v: v.user_count, reverse=True)

    # ── Крок 3: попарні перетини (ребра PyVis) ────────────────────────────────
    overlaps: dict[tuple[str, str], int] = defaultdict(int)
    for sv in shared:
        for u_a, u_b in combinations(sv.users, 2):
            key = (min(u_a, u_b), max(u_a, u_b))
            overlaps[key] += 1

    stats = {
        "scanned_dbs":   len(db_files),
        "scanned_users": scanned_users,
        "total_reposts": total_reposts,
        "unique_videos": len(video_to_users),
        "shared_videos": len(shared),
        "user_pairs":    len(overlaps),
        "errors":        errors,
    }
    logger.info(
        "cross_analyzer: знайдено %d спільних відео у %d парах",
        len(shared), len(overlaps),
    )

    return CrossAnalysisResult(
        shared=shared,
        overlaps=dict(overlaps),
        stats=stats,
    )



def find_shared_interests(project_dir: str | None = None) -> dict:
    """
    Знаходить семантичні перетини інтересів між профілями.
    Витягує теги (interests) з БД кожного користувача та робить запит до Qwen
    для знаходження спільних контекстів.
    """
    base_dir = project_dir or os.getcwd()
    pattern  = os.path.join(base_dir, "databases", "osint_*.db")
    import glob
    db_files = sorted(glob.glob(pattern))

    if len(db_files) < 2:
        return {"error": "Недостатньо баз даних для семантичного аналізу (мінімум 2)."}
        
    user_profiles = {}
    import sqlite3
    for db_file in db_files:
        username = _username_from_path(db_file)
        try:
            conn = sqlite3.connect(db_file)
            rows = conn.execute("SELECT interests FROM analysis WHERE interests IS NOT NULL AND interests != ''").fetchall()
            conn.close()
            
            all_interests = set()
            for r in rows:
                # interests зазвичай зберігаються як строка через кому або json
                for item in str(r[0]).replace('[', '').replace(']', '').replace('"', '').replace("'", '').split(","):
                    item = item.strip()
                    if item:
                        all_interests.add(item)
            if all_interests:
                user_profiles[username] = ", ".join(list(all_interests)[:50]) # Беремо топ 50 щоб не переповнити контекст
        except Exception as e:
            logger.error("Помилка збору інтересів з %s: %s", db_file, e)

    if len(user_profiles) < 2:
        return {"error": "Не знайдено достатньо інтересів у базах для порівняння."}

    # Виклик LLM
    import requests
    try:
        import config
    except ImportError:
        return {"error": "Не знайдено файл config.py для налаштувань LLM."}
        
    prompt = "Ти — досвідчений OSINT-аналітик. Проаналізуй наступні списки інтересів та тегів користувачів TikTok. Знайди їхні спільні теми, семантичні перетини (наприклад, обидва цікавляться криптою або схожими хобі) та спільні психологічні тригери. Виведи результат у форматі Markdown (список спільних рис).\n\n"
    for user, interests in user_profiles.items():
        prompt += f"Користувач @{user}: {interests}\n"

    payload = {
        "model": getattr(config, "OLLAMA_MODEL", "qwen2.5"),
        "prompt": prompt,
        "stream": False
    }

    try:
        res = requests.post(
            getattr(config, "OLLAMA_URL", "http://localhost:11434/api/generate"), 
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=120
        )
        if res.status_code == 200:
            return {
                "report": res.json().get("response", "").strip(),
                "users": list(user_profiles.keys())
            }
        else:
            return {"error": f"LLM повернула статус {res.status_code}"}
    except Exception as e:
        return {"error": f"Помилка виклику LLM Qwen: {e}"}

# ══════════════════════════════════════════════════════════════════════════════

# Backward-compat helpers (використовуються у app.py)
# ══════════════════════════════════════════════════════════════════════════════

def result_to_dataframe(result: CrossAnalysisResult) -> pd.DataFrame:
    """Alias → result.to_pairs_df() для зворотної сумісності."""
    return result.to_pairs_df()


def overlaps_to_dataframe(result: CrossAnalysisResult) -> pd.DataFrame:
    """
    DataFrame пар (Профіль A / Профіль B / Спільних відео).
    Використовується у TAB 5 для таблиці-підсумку.
    """
    if not result.overlaps:
        return pd.DataFrame(
            columns=["Профіль A", "Профіль B", "Спільних відео"]
        )
    rows = [
        {
            "Профіль A":      f"@{u_a}",
            "Профіль B":      f"@{u_b}",
            "Спільних відео": count,
        }
        for (u_a, u_b), count in sorted(
            result.overlaps.items(), key=lambda x: x[1], reverse=True
        )
    ]
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Утиліти
# ══════════════════════════════════════════════════════════════════════════════

def _username_from_path(db_path: str) -> str:
    """osint_alice.db → 'alice'"""
    return os.path.basename(db_path).replace("osint_", "").replace(".db", "")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    res = find_shared_reposts()
    print("\n=== СТАТИСТИКА ===")
    print(json.dumps(res.stats, ensure_ascii=False, indent=2))

    if res.shared:
        print(f"\n=== СПІЛЬНІ ВІДЕО (топ-10 з {len(res.shared)}) ===")
        for sv in res.shared[:10]:
            print(f"  {sv.video_id}  →  {sv.users_display}")

    if res.overlaps:
        print("\n=== ПЕРЕТИНИ (попарно) ===")
        for (a, b), cnt in sorted(res.overlaps.items(), key=lambda x: x[1], reverse=True):
            print(f"  @{a}  ↔  @{b}  : {cnt} відео")

    print("\n=== MALTEGO CSV (preview) ===")
    print(res.to_maltego_df().to_string(index=False))

    print("\n=== PAIRS DF (preview) ===")
    print(res.to_pairs_df().to_string(index=False))
