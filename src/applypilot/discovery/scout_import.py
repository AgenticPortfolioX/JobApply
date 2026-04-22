"""Importer for jobs from the external JobSearch (Scout) database."""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

from applypilot.config import APP_DIR
from applypilot.database import get_connection, store_jobs

log = logging.getLogger(__name__)

# The Scout DB is one level up from ApplyPilot-Custom
SCOUT_DB_PATH = APP_DIR.parent / "jobs.db"
REJECT_KEYWORDS = ["nurse", "dialysis", "patient care", "technician", "medical", "clinical"]

def run_scout_import() -> dict:
    """Read jobs from the external JobSearch Scout database and import into ApplyPilot."""
    if not SCOUT_DB_PATH.exists():
        log.warning("Scout database not found at %s. Skipping import.", SCOUT_DB_PATH)
        return {"new": 0, "duplicates": 0, "status": "skipped"}

    log.info("Importing jobs from Scout database: %s", SCOUT_DB_PATH)
    
    try:
        scout_conn = sqlite3.connect(str(SCOUT_DB_PATH))
        scout_conn.row_factory = sqlite3.Row
        
        # Only get jobs that haven't been applied to yet in the Scout DB
        rows = scout_conn.execute(
            "SELECT company, title, link FROM jobs WHERE applied = 0"
        ).fetchall()
        scout_conn.close()
        
        if not rows:
            log.info("No new jobs found in Scout database.")
            return {"new": 0, "duplicates": 0, "status": "ok"}
            
        # Convert to ApplyPilot format
        jobs_to_import = []
        for row in rows:
            title = row["title"]
            if any(k in title.lower() for k in REJECT_KEYWORDS):
                log.info("Scout Import: Skipping irrelevant job: %s", title)
                continue
                
            jobs_to_import.append({
                "url": row["link"],
                "title": title,
                "site": row["company"],
                "description": "", # Scout doesn't have descriptions, will be filled by Enrichment stage
                "location": ""     # Scout doesn't have location in DB, but has title etc.
            })
            
        # Store in ApplyPilot DB
        ap_conn = get_connection()
        new_count, dup_count = store_jobs(
            ap_conn, 
            jobs_to_import, 
            site="Scout-JobSearch", 
            strategy="scout_import"
        )
        
        log.info("Scout Import: %d new jobs added, %d duplicates skipped.", new_count, dup_count)
        return {"new": new_count, "duplicates": dup_count, "status": "ok"}
        
    except Exception as e:
        log.error("Failed to import from Scout database: %s", e)
        return {"new": 0, "duplicates": 0, "status": f"error: {e}"}

if __name__ == "__main__":
    # Test run
    logging.basicConfig(level=logging.INFO)
    print(run_scout_import())
