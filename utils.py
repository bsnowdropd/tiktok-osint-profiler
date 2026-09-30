import os
import re

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(PROJECT_DIR, "databases")
REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")

# Auto-create directories upon import
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

def sanitize_username(user: str) -> str:
    """Removes @ and invalid characters from the username."""
    clean = user.replace("@", "").strip()
    # Keep only letters, numbers, dots, dashes, and underscores
    clean = re.sub(r"[^a-zA-Z0-9._-]", "", clean)
    if not clean:
        raise ValueError("Invalid username format")
    return clean

def db_path_for(username: str) -> str:
    """Returns the absolute path to the OSINT SQLite database for the given user."""
    safe_user = sanitize_username(username)
    return os.path.join(DB_DIR, f"osint_{safe_user}.db")

def report_path_for(username: str, extension: str = "txt") -> str:
    """Returns the absolute path for the generated report (TXT or PDF)."""
    safe_user = sanitize_username(username)
    ext = extension.lstrip(".")
    return os.path.join(REPORTS_DIR, f"profile_{safe_user}.{ext}")
