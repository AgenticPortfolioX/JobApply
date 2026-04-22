import logging
import sqlite3
from pathlib import Path
from applypilot import database

logger = logging.getLogger(__name__)

def sync_daily_jobs() -> tuple[int, int]:
    """Synchronize jobs from the external jobs.db into ApplyPilot.
    
    Reads from ../jobs.db, maps fields to the ApplyPilot format,
    and inserts using the standard store_jobs method to handle deduplication.
    
    Returns:
        tuple[int, int]: (new_jobs_added, duplicate_jobs_skipped)
    """
    # Assuming ApplyPilot is in an immediate subdirectory of jobsearch
    external_db_path = Path(__file__).resolve().parents[3] / "jobs.db"
    
    if not external_db_path.exists():
        logger.error(f"External job database not found at {external_db_path}")
        return 0, 0
        
    try:
        # Read from external DB
        # read-only connection to avoid locking issues
        ext_uri = f"file:{external_db_path}?mode=ro"
        ext_conn = sqlite3.connect(ext_uri, uri=True)
        ext_conn.row_factory = sqlite3.Row
        
        cursor = ext_conn.cursor()
        cursor.execute("SELECT id, job_hash, company, title, link, date_seen FROM jobs")
        rows = cursor.fetchall()
        ext_conn.close()
        
    except sqlite3.Error as e:
        logger.error(f"Failed to read from external jobs.db: {e}")
        return 0, 0
        
    jobs_to_import = []
    for row in rows:
        job = {
            "url": row["link"],
            "title": row["title"],
            "site": row["company"] or "Daily Tracker",
            "discovered_at": row["date_seen"],
            "strategy": "daily_pull_import",
            "description": None,
            "salary": None,
            "location": None
        }
        jobs_to_import.append(job)

    if not jobs_to_import:
        logger.info("No jobs found to sync.")
        return 0, 0
        
    pilot_conn = database.get_connection()
    pilot_conn.execute("BEGIN TRANSACTION")
    new = 0
    existing = 0
    try:
        for job in jobs_to_import:
            if not job["url"]:
                continue
            try:
                pilot_conn.execute(
                    "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job["url"], 
                        job["title"], 
                        job["salary"], 
                        job["description"],
                        job["location"], 
                        job["site"], 
                        job["strategy"], 
                        job["discovered_at"]
                    ),
                )
                new += 1
            except sqlite3.IntegrityError:
                existing += 1
        pilot_conn.commit()
    except Exception as e:
        pilot_conn.rollback()
        logger.error(f"Failed to write to ApplyPilot db: {e}")
        return 0, 0
    finally:
        database.close_connection()
        
    return new, existing
