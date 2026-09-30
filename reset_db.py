"""
reset_db.py — Utility to reset the AI analysis status for a profile.

Does NOT delete scraped reposts or downloaded videos.
Only clears the `analysis` table so the AI pipeline can run again.
"""

import sqlite3
import argparse
import logging
import sys
import os

from utils import db_path_for, sanitize_username

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def reset_analysis(db_path: str, confirm: bool = False) -> None:
    """
    Clears all data from the `analysis` table.
    
    Args:
        db_path: Path to osint_<username>.db.
        confirm: If False, will prompt for user input before deletion.
    """
    if not os.path.exists(db_path):
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    if not confirm:
        logger.warning("⚠️  DESTRUCTIVE OPERATION: This will delete all analysis results in %s!", db_path)
        ans = input("Type 'yes' to confirm: ")
        if ans.lower() != "yes":
            logger.info("Operation cancelled.")
            sys.exit(0)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM analysis")
        count = cursor.fetchone()[0]
        
        cursor.execute("DELETE FROM analysis")
        conn.commit()
        conn.close()
        
        logger.info("✅ Successfully deleted %d records from the 'analysis' table.", count)
        logger.info("You can now re-run run_pipeline.py for this profile.")
        
    except sqlite3.Error as e:
        logger.error("SQLite Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset AI analysis results for a TikTok OSINT profile.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--user", 
        help="TikTok username (script will locate databases/osint_<user>.db)"
    )
    group.add_argument(
        "--db", 
        help="Direct path to the .db file (e.g., databases/osint_user.db)"
    )
    parser.add_argument(
        "-y", "--yes", 
        action="store_true", 
        help="Skip confirmation prompt"
    )
    
    args = parser.parse_args()
    
    if args.user:
        target_db = db_path_for(args.user)
    else:
        target_db = args.db
        
    reset_analysis(target_db, confirm=args.yes)
