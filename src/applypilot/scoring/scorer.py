"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator specializing in compliance, financial operations, audit, and regulatory roles. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct, hands-on experience in nearly all required responsibilities and domain knowledge.
- 7-8: Strong match. Candidate has most required skills and domain knowledge; minor gaps are easily bridged by adjacent experience.
- 5-6: Moderate match. Candidate has transferable skills and relevant background but is missing 1-2 key requirements.
- 3-4: Weak match. Candidate's background is in a related field but significant gaps exist in required domain knowledge.
- 1-2: Poor match. Completely different function or career path from the role requirements.

IMPORTANT FACTORS — WEIGHT IN THIS ORDER:
1. Domain expertise match (e.g., AML/BSA → compliance role; Accounting/GAAP → accountant role; MBS/corporate advance → loan servicing role)
2. Years of relevant experience vs. job requirements (seniority level, 10+ years in banking/finance = senior candidate)
3. Regulatory/certification alignment (CAMS, MBA, BBA, specific regulator experience like FinCEN, FNMA, GNMA, audit readiness)
4. Transferable operational skills (audit fulfillment, process improvement, reconciliation, reporting, ledger management)
5. Technology & tools (Tableau, Excel, Salesforce, workflow automation)

DO NOT penalize candidates for lacking domain-specific certs (like CAMS) if the job is for a different domain (like general Accounting).
DO NOT penalize compliance or finance candidates for lacking software programming skills unless the job explicitly requires coding as a core duty.
DO give credit for AI-assisted development experience, workflow automation (N8N, Antigravity), and process optimization.
DO give credit for agentic/compliance boundary-crossing projects (e.g., on-chain compliance, tokenization, smart contract governance).
DO recognize that institutional banking experience (Fannie Mae, Freddie Mac, FinCEN, Flagstar) is highly specialized and transferable.

RESPOND IN EXACTLY THIS FORMAT (no other text, no preamble, no commentary):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score, citing specific matching qualifications]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=8192, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def route_base_resume(job: dict, profile: dict) -> str:
    """Select the best base resume for a job using the LLM."""
    base_resumes = profile.get("base_resumes", {})
    if not base_resumes:
        return "default"
    
    keys = list(base_resumes.keys())
    if len(keys) == 1:
        return keys[0]
        
    # Pull selection strategy and keywords from profile
    ai_logic = profile.get("ai_logic", {})
    strategy = ai_logic.get("selection_strategy", "")
    mapping = ai_logic.get("keyword_mapping", {})
    
    prompt = f"You are a routing agent for a job application pipeline.\nYou must choose the single best BASE RESUME to use for the following job description.\n\n"
    
    if strategy:
        prompt += f"FOLLOW THIS STRATEGY:\n{strategy}\n\n"
        
    prompt += "AVAILABLE RESUME VARIANTS:\n"
    for k in keys:
        keywords = ", ".join(mapping.get(k, []))
        prompt += f"- {k} (Key areas: {keywords})\n"
        
    prompt += f"\nJOB TITLE: {job.get('title')}\nCOMPANY: {job.get('site')}\nDESCRIPTION:\n{(job.get('full_description') or '')[:4000]}\n\nRespond with exactly ONE string from the AVAILABLE RESUME VARIANTS list that best matches the job requirements. Return ONLY the exact name of the resume, no other text, no reasoning, no explanation."

    try:
        client = get_client()
        response = client.chat([{"role": "user", "content": prompt}], max_tokens=2048, temperature=0.1)
        selected = response.strip()
        for k in keys:
            if k.lower() in selected.lower():
                return k
        return keys[0]
    except Exception as e:
        log.error("LLM error routing base resume: %s", e)
        return keys[0]


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 5) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Number of parallel threads for scoring.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    profile = load_profile()
    base_resumes = profile.get("base_resumes", {})
    default_resume_text = ""
    if RESUME_PATH.exists():
        default_resume_text = RESUME_PATH.read_text(encoding="utf-8")
    
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs_rows = conn.execute(query).fetchall()
    else:
        jobs_rows = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs_rows:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts
    jobs = [dict(row) for row in jobs_rows]

    log.info("Scoring %d jobs using %d workers...", len(jobs), workers)
    t0 = time.time()
    completed = 0
    errors = 0
    
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process_job(job):
        """Worker task: route, score, and return result."""
        # Note: We don't use the shared 'conn' here to avoid thread safety issues
        base_resume_key = route_base_resume(job, profile)
        if base_resumes and base_resume_key in base_resumes:
            resume_text = base_resumes[base_resume_key]
        else:
            resume_text = default_resume_text
            
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        result["base_resume_key"] = base_resume_key
        result["title"] = job.get("title", "?")
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_job = {executor.submit(_process_job, job): job for job in jobs}
        
        for future in as_completed(future_to_job):
            try:
                result = future.result()
                completed += 1
                
                if result["score"] == 0:
                    errors += 1

                # Update DB immediately for real-time GUI feedback
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, base_resume_key = ? WHERE url = ?",
                    (result["score"], f"{result['keywords']}\n{result['reasoning']}", now, result["base_resume_key"], result["url"]),
                )
                conn.commit()

                log.info(
                    "[%d/%d] score=%d  %s (%s)",
                    completed, len(jobs), result["score"], result["title"][:60], result["url"]
                )
            except Exception as e:
                log.error("Worker failed: %s", e)
                errors += 1

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", completed, elapsed, completed / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": completed,
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
