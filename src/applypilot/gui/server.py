"""FastAPI server for ApplyPilot Control Center."""

import asyncio
import sys
import os
import json
import shutil
import re
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import applypilot
from applypilot import config
from applypilot.database import get_connection, get_stats
from applypilot.gui.process_manager import ProcessManager

app = FastAPI(title="ApplyPilot API")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Process Manager
pm = ProcessManager()

# Ensure directories
config.ensure_dirs()
GUI_DIR = Path(__file__).parent
WEB_DIR = GUI_DIR / "web"
WEB_DIR.mkdir(exist_ok=True)

# Mount static directories
app.mount("/api/files/review", StaticFiles(directory=config.REVIEW_DIR), name="review_files")
app.mount("/api/files/tailored", StaticFiles(directory=config.TAILORED_DIR), name="tailored_files")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")

# --- WebSocket connection manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

@app.on_event("startup")
async def startup_event():
    # Start the process manager event loop
    asyncio.create_task(pm.listen_and_broadcast(manager.broadcast))

@app.on_event("shutdown")
async def shutdown_event():
    await pm.stop()

# --- API Endpoints ---

@app.get("/api/status")
async def get_pipeline_status():
    """Return funnel stats."""
    from applypilot.database import get_stats
    return get_stats()

@app.get("/api/files/review")
async def list_review_files():
    """List jobs currently in the Review folder, with optional cover letter matches."""
    review_dir = config.REVIEW_DIR
    cover_dir = config.COVER_LETTER_DIR
    if not review_dir.exists():
        return []

    jobs = {}
    # Scan Review dir for resumes and job descriptions
    for f in review_dir.iterdir():
        if not f.is_file():
            continue
        if f.name.endswith("_JOB.txt"):
            prefix = f.name[:-8]
            entry = jobs.setdefault(prefix, {"prefix": prefix})
            entry["job_file"] = f.name
        elif f.name.endswith("_REPORT.json") or f.name.endswith("_REPORT.txt"):
            continue
        else:
            prefix = f.stem
            entry = jobs.setdefault(prefix, {"prefix": prefix})
            entry["resume_file"] = f.name

    # Cross-reference cover letters directory by prefix
    if cover_dir.exists():
        for f in cover_dir.iterdir():
            if not f.is_file():
                continue
            prefix = f.stem
            if prefix in jobs:
                jobs[prefix]["cover_file"] = f.name

    return list(jobs.values())

@app.post("/api/files/approve/{prefix}")
async def approve_review_file(prefix: str):
    """Move job from Review to Tailored, update DB, then auto-launch the apply agent."""
    review_dir = config.REVIEW_DIR
    tailored_dir = config.TAILORED_DIR
    tailored_dir.mkdir(parents=True, exist_ok=True)

    job_file = review_dir / f"{prefix}_JOB.txt"
    if not job_file.exists():
        raise HTTPException(status_code=404, detail="Job description file not found")

    # Extract URL from job file
    job_text = job_file.read_text(encoding="utf-8")
    match = re.search(r"URL: (https?://\S+)", job_text)
    if not match:
        raise HTTPException(status_code=400, detail="Could not find URL in job file")
    url = match.group(1)

    # Move all files with this prefix from Review to Tailored
    moved_resume_path = None
    for f in list(review_dir.glob(f"{prefix}*")):
        target = tailored_dir / f.name
        shutil.move(str(f), str(target))
        if f.name == f"{prefix}.txt":
            moved_resume_path = str(target)

    if not moved_resume_path:
        for f in tailored_dir.glob(f"{prefix}*.txt"):
            if not f.name.endswith("_JOB.txt") and not f.name.endswith("_REPORT.txt"):
                moved_resume_path = str(f)
                break

    # Update DB
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET tailored_resume_path=?, tailored_at=? WHERE url=?",
        (moved_resume_path, now, url)
    )
    conn.commit()

    # Auto-launch the apply agent for this specific job
    # Allow multiple processes now
    process_id = f"apply_{prefix}"
    if pm.is_running(process_id):
        return {"status": "approved", "url": url, "agent": "skipped", "reason": "already running"}

    cmd = [sys.executable, "-m", "applypilot.cli", "apply", "--url", url, "--pause-for-approval"]
    await pm.start(cmd, process_id=process_id)

    return {"status": "approved", "url": url, "agent": "launched", "process_id": process_id}

@app.post("/api/files/reject/{prefix}")
async def reject_review_file(prefix: str):
    """Delete files from Review folder."""
    review_dir = config.REVIEW_DIR
    for f in review_dir.glob(f"{prefix}*"):
        f.unlink()
    return {"status": "rejected"}

@app.get("/api/files/download/{filename}")
async def download_review_file(filename: str):
    """Download or view a file from Review dir."""
    file_path = config.REVIEW_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Force text/plain for .txt files so they render in iframes
    if filename.endswith(".txt"):
        return FileResponse(file_path, media_type="text/plain")
    return FileResponse(file_path)

@app.get("/api/files/cover/{filename}")
async def serve_cover_letter(filename: str):
    """Serve a cover letter file from the cover_letters directory."""
    file_path = config.COVER_LETTER_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Cover letter not found")
    return FileResponse(file_path)

@app.get("/api/queue")
async def get_execution_queue():
    """Get jobs ready for execution (tailored, not yet applied)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, title, site, fit_score, applied_at, apply_status, apply_error, tailored_resume_path, cover_letter_path
        FROM jobs 
        WHERE tailored_resume_path IS NOT NULL 
          AND applied_at IS NULL
          AND fit_score >= 6
        ORDER BY fit_score DESC 
        LIMIT 50
    """).fetchall()
    return [dict(row) for row in rows]

class DiscardRequest(BaseModel):
    url: str

@app.post("/api/queue/discard")
async def discard_job(req: DiscardRequest):
    """Remove a job from the execution queue and delete its tailored files."""
    conn = get_connection()
    job = conn.execute(
        "SELECT tailored_resume_path, cover_letter_path FROM jobs WHERE url=?",
        (req.url,)
    ).fetchone()
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Delete files
    for path_str in [job["tailored_resume_path"], job["cover_letter_path"]]:
        if path_str:
            p = Path(path_str)
            if p.exists():
                try:
                    p.unlink()
                    # Also try to delete associated PDF if it's a resume
                    if "tailored_resumes" in path_str and p.suffix == ".txt":
                        pdf = p.with_suffix(".pdf")
                        if pdf.exists():
                            pdf.unlink()
                except Exception as e:
                    print(f"Error deleting file {p}: {e}")

    # Reset DB fields
    conn.execute(
        """UPDATE jobs SET 
           tailored_resume_path=NULL, tailored_at=NULL, 
           cover_letter_path=NULL, cover_letter_at=NULL,
           apply_status=NULL, apply_error=NULL
           WHERE url=?""",
        (req.url,)
    )
    conn.commit()
    return {"status": "discarded"}

# Process control schemas
class LaunchRequest(BaseModel):
    command: str
    args: List[str] = []
    process_id: Optional[str] = None  # Stable caller-supplied ID (prevents bad IDs from arg parsing)

class InputRequest(BaseModel):
    input_text: str

@app.post("/api/process/launch")
async def launch_process(req: LaunchRequest):
    """Launch a long-running subprocess."""
    # Use caller-supplied process_id if provided, otherwise fall back to command name
    # NEVER use args[0] as process_id — it resolves to the first flag (e.g. '--limit')
    process_id = req.process_id or req.command
    if pm.is_running(process_id):
        raise HTTPException(status_code=400, detail=f"Process {process_id} is already running.")
    
    # Restrict to applypilot commands for safety
    if req.command not in ["run", "apply", "doctor", "sync"]:
        raise HTTPException(status_code=400, detail="Invalid command.")
        
    cmd = [sys.executable, "-m", "applypilot.cli", req.command] + req.args
    await pm.start(cmd, process_id=process_id)
    return {"status": "started", "process_id": process_id}

@app.post("/api/process/input/{process_id}")
async def send_process_input(process_id: str, req: InputRequest):
    """Send standard input (e.g. 'y\n') to running process."""
    if not pm.is_running(process_id):
        raise HTTPException(status_code=400, detail=f"Process {process_id} is not running.")
    
    await pm.write_stdin(req.input_text, process_id=process_id)
    return {"status": "sent"}

@app.post("/api/process/stop/{process_id}")
async def stop_process(process_id: str):
    await pm.stop(process_id)
    return {"status": "stopped"}

@app.post("/api/process/stop_all")
async def stop_all_processes():
    await pm.stop()
    return {"status": "stopped_all"}

@app.post("/api/process/reset/{stage}")
async def reset_stage(stage: str):
    """Reset a specific stage by clearing relevant database fields."""
    conn = get_connection()
    try:
        if stage == "discovery":
            # Clear all jobs
            conn.execute("DELETE FROM jobs")
        elif stage == "scoring":
            # Reset scores
            conn.execute("UPDATE jobs SET fit_score = NULL")
        elif stage == "tailoring":
            # Reset tailoring and cover letter paths
            conn.execute("UPDATE jobs SET tailored_resume_path = NULL, cover_letter_path = NULL, tailored_at = NULL")
        else:
            raise HTTPException(status_code=400, detail="Invalid stage for reset.")
        
        conn.commit()
        return {"status": "reset", "stage": stage}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- Mount Static Frontend ---
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("applypilot.gui.server:app", host="0.0.0.0", port=8000, reload=False)

