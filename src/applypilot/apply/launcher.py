"""Apply orchestration: acquire jobs, launch Gemini-powered workers, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Gemini Driver for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

# known aggregator/spam/dead-end sites that shouldn't be processed
BLOCKLIST_DOMAINS = {
    "flexionis.wuaze.com",
    "remotejobs.victorytuitions.in",
    "victorytuitions.in"
}

def is_blocklisted(url: str) -> bool:
    if not url:
        return False
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc.lower()
    for domain in BLOCKLIST_DOMAINS:
        if netloc == domain or netloc.endswith("." + domain):
            return True
    return False

def _prompt_user_approval(port: int, job: dict):
    print(f"\n\n==================================================")
    print(f"PAUSED FOR APPROVAL: {job['title']} at {job.get('site')}")
    print(f"Please inspect Chrome on port {port}.")
    print(f"Type 'y' to SUBMIT, or 'n' to REJECT, then press Enter: ", end="", flush=True)
    
    ans = sys.stdin.readline().strip().lower()
    
    # Send a request to the browser if needed, or rely on the user to manually click
    if ans == 'y':
        logger.info("User approved submission.")
        # We assume user clicked submit in the UI
    else:
        logger.info("User rejected submission.")

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active worker processes/threads
_active_workers: dict[int, any] = {}
_worker_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    "-y",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 6,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")

            if target_url:
                like = f"%{target_url.split('?')[0].rstrip('/')}%"
                row = conn.execute("""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                      AND tailored_resume_path IS NOT NULL
                      AND apply_status IS NULL
                    LIMIT 1
                """, (target_url, target_url, like, like)).fetchone()
            else:
                blocked_sites, blocked_patterns = _load_blocked()
                params: list = [min_score]
                site_clause = ""
                if blocked_sites:
                    placeholders = ",".join(["?"] * len(blocked_sites))
                    site_clause = f"AND site NOT IN ({placeholders})"
                    params.extend(blocked_sites)
                url_clauses = ""
                if blocked_patterns:
                    url_clauses = " ".join(f"AND (url NOT LIKE ? AND application_url NOT LIKE ?)" for _ in blocked_patterns)
                    # Extend params with two copies of each pattern (one for url, one for application_url)
                    for p in blocked_patterns:
                        params.extend([p, p])
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE tailored_resume_path IS NOT NULL
                      AND apply_status IS NULL
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND fit_score >= ?
                      {site_clause}
                      {url_clauses}
                    ORDER BY fit_score DESC, url
                    LIMIT 1
                """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

            if not row:
                conn.rollback()
                return None

            # Skip manual ATS sites (unsolvable CAPTCHAs)
            from applypilot.config import is_manual_ats
            apply_url = row["application_url"] or row["url"]
            if is_manual_ats(apply_url):
                logger.info("Skipping manual ATS: %s", row["url"][:80])
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                # LOOP AGAIN to find the next job
                if target_url: return None # No point looping on target url
                continue

            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE jobs SET apply_status = 'in_progress',
                               agent_id = ?,
                               last_attempted_at = ?
                WHERE url = ?
            """, (f"worker-{worker_id}", now, row["url"]))
            conn.commit()

            return dict(row)
        except Exception:
            conn.rollback()
            raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 6,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "gemini-flash-latest", dry_run: bool = False, pause_for_approval: bool = False) -> tuple[str, int]:
    """Launch a Gemini application driver for one job assignment.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        pause_for_approval=pause_for_approval,
    )

    # All applications now use the Gemini Vision-Action Driver
    app_url = job.get("application_url") or job.get("url")
    if is_blocklisted(app_url):
        add_event(f"[W{worker_id}] SKIPPING: Blocklisted domain ({app_url[:40]})")
        update_state(worker_id, status="failed", last_action="BLOCKLISTED_DOMAIN")
        return "failed:blocklisted_domain", 0

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting_gemini")
    add_event(f"[W{worker_id}] Starting Gemini Driver: {job['title'][:40]}")
    
    from applypilot.apply.gemini_driver import run_gemini_apply
    
    start_time = time.time()
    try:
        # The Gemini driver handles its own browser connection and action loop
        result_status, duration_ms = run_gemini_apply(
            worker_id=worker_id,
            job=job,
            tailored_resume=resume_text,
            prompt_text=agent_prompt,
            port=port,
            pause_for_approval=pause_for_approval
        )
        
        # Log and update state based on driver result
        elapsed = int(time.time() - start_time)
        display_status = result_status.split(':')[0]
        
        add_event(f"[W{worker_id}] {result_status.upper()} ({elapsed}s): {job['title'][:30]}")
        update_state(worker_id, status=display_status, 
                     last_action=f"{result_status.upper()} ({elapsed}s)")
        
        return result_status, duration_ms
        
    except Exception as e:
        logger.exception("Gemini driver fatal error")
        duration_ms = int((time.time() - start_time) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action="driver_crash")
        return f"failed:crash_{str(e)[:50]}", duration_ms
    finally:
        with _worker_lock:
            _active_workers.pop(worker_id, None)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "no_form_found", "repetition_detected", "aborted",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    if not result:
        return False
        
    result_lower = result.lower()
    # Check for direct matches or prefixed codes
    reason = result_lower.split(":", 1)[-1] if ":" in result_lower else result_lower
    
    # Also check if it's already marked as 99 in result (unlikely here but safe)
    if "99" in result_lower: return True

    return (
        result_lower in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
        or any(result_lower.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 6, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False, pause_for_approval: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            if empty_polls % 50 == 0: # Only log every 50 polls to reduce spam
                update_state(worker_id, status="idle",
                             last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run, pause_for_approval=pause_for_approval)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            import traceback
            error_msg = f"FATAL WORKER ERROR: {str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            # Log to worker file if possible
            worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
            try:
                with open(worker_log, "a", encoding="utf-8") as lf:
                    lf.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n")
            except:
                pass
            add_event(f"[W{worker_id}] CRASH: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

        # Cool-down between jobs to prevent Chrome from spawning every second
        # when jobs fail fast (e.g. same-file errors, connection issues, etc.)
        if not _stop_event.wait(timeout=5):
            pass  # 5-second pause between each job attempt

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1, pause_for_approval: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Stopping workers... (Ctrl+C again to force quit)[/yellow]")
            _stop_event.set()
        else:
            console.print("\n[red bold]EMERGENCY STOP[/red bold]")
            _stop_event.set()
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        # Disable iterative dashboard in GUI mode to prevent terminal redraw spam
        if os.environ.get("APPLYPILOT_GUI"):
            _dashboard_running = True
            add_event("[SYSTEM] Starting headless log stream...")
            
            if workers == 1:
                total_applied, total_failed = worker_loop(
                    worker_id=0, limit=effective_limit, target_url=target_url,
                    min_score=min_score, headless=headless, model=model,
                    dry_run=dry_run, pause_for_approval=pause_for_approval,
                )
            else:
                # Multi-worker logic
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0) for i in range(workers)]
                else:
                    limits = [0] * workers

                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop, i, limits[i], target_url, min_score,
                            headless, model, dry_run, pause_for_approval
                        ): i for i in range(workers)
                    }
                    results = []
                    for future in as_completed(futures):
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker crashed")
                            results.append((0, 0))
                    total_applied = sum(r[0] for r in results)
                    total_failed = sum(r[1] for r in results)
            _dashboard_running = False
        else:
            # Interactive Terminal Dashboard
            with Live(render_full(), console=console, refresh_per_second=2) as live:
                _dashboard_running = True

                def _refresh():
                    while _dashboard_running:
                        live.update(render_full())
                        time.sleep(0.5)

                refresh_thread = threading.Thread(target=_refresh, daemon=True)
                refresh_thread.start()

                if workers == 1:
                    total_applied, total_failed = worker_loop(
                        worker_id=0, limit=effective_limit, target_url=target_url,
                        min_score=min_score, headless=headless, model=model,
                        dry_run=dry_run, pause_for_approval=pause_for_approval,
                    )
                else:
                    # Multi-worker logic (same as above but with live update)
                    if effective_limit:
                        base = effective_limit // workers
                        extra = effective_limit % workers
                        limits = [base + (1 if i < extra else 0) for i in range(workers)]
                    else:
                        limits = [0] * workers

                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
                        futures = {
                            executor.submit(
                                worker_loop, i, limits[i], target_url, min_score,
                                headless, model, dry_run, pause_for_approval
                            ): i for i in range(workers)
                        }
                        results = []
                        for future in as_completed(futures):
                            try:
                                results.append(future.result())
                            except Exception:
                                logger.exception("Worker crashed")
                                results.append((0, 0))
                        total_applied = sum(r[0] for r in results)
                        total_failed = sum(r[1] for r in results)

                _dashboard_running = False
                refresh_thread.join(timeout=2)
                live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
