"""
cross_analyzer.py - Cross-analysis of shared reposts between all OSINT profiles.

Algorithm
─────────
1. Scans the directory for osint_*.db files.
2. Through DatabaseManager reads (video_id, url, author) of each profile -
   the connection is closed immediately after reading (→ no "database is locked").
3. Builds a mapping: video_id → {username_1: url, username_2: url, ...}
4. Keeps only those video_id where ≥ min_users profiles.
5. Returns CrossAnalysisResult with three data formats:
     .shared          list[SharedVideo]            - for UI cards
     .overlaps        dict[(user_a, user_b), int]  - for PyVis edges
     .to_pairs_df()   DataFrame Target_1/Target_2/Shared_Video_ID/Video_URL
     .to_maltego_df() DataFrame Source/Target/Edge_Type/Weight  (for Maltego)

Usage
────────────
    from cross_analyzer import find_shared_reposts

    result = find_shared_reposts(project_dir="/abs/path")

    # For AgGrid / st.dataframe
    df_pairs   = result.to_pairs_df()
    df_maltego = result.to_maltego_df()

    # For PyVis graph
    for (u_a, u_b), count in result.overlaps.items():
        G.add_edge(u_a, u_b, weight=count, label="Shared content")
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
# Data Models
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SharedVideo:
    """A video that has been reposted by two or more targets."""
    video_id: str
    url:      str
    users:    list[str]   # target nicknames in alphabetical order

    @property
    def user_count(self) -> int:
        return len(self.users)

    @property
    def users_display(self) -> str:
        return " · ".join(f"@{u}" for u in self.users)


@dataclass
class CrossAnalysisResult:
    """
    Result of the cross-analysis of all databases.

    Attributes
    ----------
    shared   : list[SharedVideo]
    overlaps : dict[tuple[str, str], int]
        Pairs (user_a, user_b) → number of shared videos.
        Key is always alphabetically sorted: user_a < user_b.
    stats    : dict
        Overall scanning statistics.
    """
    shared:   list[SharedVideo]          = field(default_factory=list)
    overlaps: dict[tuple[str, str], int] = field(default_factory=dict)
    stats:    dict                        = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return len(self.shared) == 0

    def users_with_overlap(self) -> set[str]:
        """All nicknames that have at least one shared repost."""
        out: set[str] = set()
        for u_a, u_b in self.overlaps:
            out.add(u_a)
            out.add(u_b)
        return out

    # ── DataFrame Converters ────────────────────────────────────────────────

    def to_pairs_df(self) -> pd.DataFrame:
        """
        DataFrame for AgGrid: each row is a pair of targets + video.

        Columns
        -------
        Target_1      : str   - nickname of the first target
        Target_2      : str   - nickname of the second target
        Shared_Video_ID : str - TikTok video_id
        Video_URL     : str   - video link

        Note: if a video was reposted by N>2 targets, it will appear in C(N,2)
        rows (for each pair). This allows filtering by Target_1/Target_2.
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
        # Sort: most active pairs first
        pair_counts = df.groupby(["Target_1", "Target_2"]).size().rename("_cnt")
        df = df.merge(pair_counts, on=["Target_1", "Target_2"])
        df = df.sort_values(["_cnt", "Target_1", "Target_2"], ascending=[False, True, True])
        df = df.drop(columns=["_cnt"]).reset_index(drop=True)
        return df

    def to_maltego_df(self) -> pd.DataFrame:
        """
        DataFrame in Maltego CSV import format.

        Columns
        -------
        Source     : str   - nickname 1 (target)
        Target     : str   - nickname 2 (target)
        Edge_Type  : str   - always "Shared_Repost"
        Weight     : int   - number of shared videos between the pair

        Usage in Maltego
        ──────────────────────
        Import → Import from CSV → Map columns:
          Source    → Entity 1 (Person / Social Media)
          Target    → Entity 2
          Edge_Type → Link Label
          Weight    → Link Thickness (or Bookmark)
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
        """Returns CSV as bytes for st.download_button."""
        df = self.to_maltego_df()
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8")
        return buf.getvalue().encode("utf-8")

    def to_pairs_csv_bytes(self) -> bytes:
        """Returns the full pairs-CSV as bytes for st.download_button."""
        df = self.to_pairs_df()
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8")
        return buf.getvalue().encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Main Function
# ══════════════════════════════════════════════════════════════════════════════

def find_shared_reposts(
    project_dir: str | None = None,
    min_users: int = 2,
) -> CrossAnalysisResult:
    """
    Finds videos reposted by ≥ `min_users` different targets (profiles).

    Parameters
    ----------
    project_dir : str | None
        Directory to search for osint_*.db. If None - current working directory.
    min_users : int
        Minimum number of profiles to consider a video "shared".

    Returns
    -------
    CrossAnalysisResult
    """
    base_dir = project_dir or os.getcwd()
    pattern  = os.path.join(base_dir, "databases", "osint_*.db")
    db_files = sorted(glob.glob(pattern))

    if not db_files:
        logger.warning("cross_analyzer: no osint_*.db databases found in %s", base_dir)
        return CrossAnalysisResult(stats={"error": "No databases found"})

    logger.info("cross_analyzer: scanning %d databases...", len(db_files))

    # ── Step 1: video_id → {username: url} ───────────────────────────────────
    video_to_users: dict[str, dict[str, str]] = defaultdict(dict)
    scanned_users: list[str] = []
    total_reposts  = 0
    errors: list[str] = []

    for db_file in db_files:
        username = _username_from_path(db_file)
        mgr = DatabaseManager(db_file)

        try:
            # Connection opens and closes inside the method
            triples = mgr.get_video_ids_with_author()
        except Exception as exc:
            msg = f"Error reading {os.path.basename(db_file)}: {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

        scanned_users.append(username)
        total_reposts += len(triples)

        for video_id, url, _author in triples:
            if video_id:
                # Map key - target nickname (not the video author)
                video_to_users[video_id][username] = url

    # ── Step 2: filter ≥ min_users ───────────────────────────────────────
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

    # ── Step 3: pairwise overlaps (PyVis edges) ────────────────────────────────
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
        "cross_analyzer: found %d shared videos across %d pairs",
        len(shared), len(overlaps),
    )

    return CrossAnalysisResult(
        shared=shared,
        overlaps=dict(overlaps),
        stats=stats,
    )



def find_shared_interests(project_dir: str | None = None) -> dict:
    """
    Finds semantic overlaps in interests between profiles.
    Extracts tags (interests) from each user's DB and queries Qwen
    to find shared contexts.
    """
    base_dir = project_dir or os.getcwd()
    pattern  = os.path.join(base_dir, "databases", "osint_*.db")
    import glob
    db_files = sorted(glob.glob(pattern))

    if len(db_files) < 2:
        return {"error": "Not enough databases for semantic analysis (minimum 2)."}
        
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
                # interests are usually stored as a comma-separated string or json
                for item in str(r[0]).replace('[', '').replace(']', '').replace('"', '').replace("'", '').split(","):
                    item = item.strip()
                    if item:
                        all_interests.add(item)
            if all_interests:
                user_profiles[username] = ", ".join(list(all_interests)[:50]) # Take top 50 to avoid overflowing the context
        except Exception as e:
            logger.error("Error gathering interests from %s: %s", db_file, e)

    if len(user_profiles) < 2:
        return {"error": "Not enough interests found in databases for comparison."}

    # LLM Call
    import requests
    try:
        import config
    except ImportError:
        return {"error": "config.py file not found for LLM settings."}
        
    prompt = "You are an experienced OSINT analyst. Analyze the following lists of interests and tags from TikTok users. Find their common themes, semantic overlaps (e.g., both are interested in crypto or similar hobbies), and shared psychological triggers. Output the result in Markdown format (a list of common traits).\n\n"
    for user, interests in user_profiles.items():
        prompt += f"User @{user}: {interests}\n"

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
            return {"error": f"LLM returned status {res.status_code}"}
    except Exception as e:
        return {"error": f"Error calling Qwen LLM: {e}"}

# ══════════════════════════════════════════════════════════════════════════════

# Backward-compat helpers (used in app.py)
# ══════════════════════════════════════════════════════════════════════════════

def result_to_dataframe(result: CrossAnalysisResult) -> pd.DataFrame:
    """Alias → result.to_pairs_df() for backward compatibility."""
    return result.to_pairs_df()


def overlaps_to_dataframe(result: CrossAnalysisResult) -> pd.DataFrame:
    """
    DataFrame of pairs (Profile A / Profile B / Shared videos).
    Used in TAB 5 for the summary table.
    """
    if not result.overlaps:
        return pd.DataFrame(
            columns=["Profile A", "Profile B", "Shared videos"]
        )
    rows = [
        {
            "Profile A":      f"@{u_a}",
            "Profile B":      f"@{u_b}",
            "Shared videos": count,
        }
        for (u_a, u_b), count in sorted(
            result.overlaps.items(), key=lambda x: x[1], reverse=True
        )
    ]
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════════════

def _username_from_path(db_path: str) -> str:
    """osint_alice.db → 'alice'"""
    return os.path.basename(db_path).replace("osint_", "").replace(".db", "")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s")

    res = find_shared_reposts()
    print("\n=== STATISTICS ===")
    print(json.dumps(res.stats, ensure_ascii=False, indent=2))

    if res.shared:
        print(f"\n=== SHARED VIDEOS (top 10 of {len(res.shared)}) ===")
        for sv in res.shared[:10]:
            print(f"  {sv.video_id}  →  {sv.users_display}")

    if res.overlaps:
        print("\n=== OVERLAPS (pairwise) ===")
        for (a, b), cnt in sorted(res.overlaps.items(), key=lambda x: x[1], reverse=True):
            print(f"  @{a}  ↔  @{b}  : {cnt} videos")

    print("\n=== MALTEGO CSV (preview) ===")
    print(res.to_maltego_df().to_string(index=False))

    print("\n=== PAIRS DF (preview) ===")
    print(res.to_pairs_df().to_string(index=False))
