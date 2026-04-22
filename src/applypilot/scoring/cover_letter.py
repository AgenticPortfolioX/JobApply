"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Write a cover letter for Justin Gramke MBA CAMS. The goal is to get an interview.

PERSONA:
You are a Strategic Systems Architect specializing in FinTech, Risk Management, and Technical Integration. Your core philosophy is "Compliance as Code"—you don't just interpret policy; you encode it directly into the technical stack to eliminate manual friction. Your tone is direct, high-agency, and strictly professional.

INPUT DATA:
(The Job Description and Resume are provided in the next message).
User Skill Weights / Boundaries: {skills_str}
Key Projects & Metrics: {projects_hint}{metrics_hint}

STRUCTURE & CONSTRAINTS:

Length: 3 cohesive paragraphs. Under 225 words.

Tone: Direct, authoritative, zero fluff. No "I am thrilled to apply" or "I believe I am a good fit."

PARAGRAPH 1: THE BOTTLENECK HOOK
Identify the core technical or regulatory friction point implied by the JD.
Example: "Institutional finance is currently bottlenecked by the gap between static regulatory policy and dynamic transaction flow. I bridge this gap by architecting systems where compliance is an automated, real-time enforcement layer rather than a manual audit after-thought."

PARAGRAPH 2: THE ENFORCEMENT LAYER (Impact)
Use the provided skills and metrics to select 2-3 high-impact technologies.

Dynamic Logic:
If the JD mentions Audit/Accounting/Finance, use metrics like "multi-billion dollar portfolio oversight" and "100% adherence to MBS standards."
If the JD mentions AML/Tech/Blockchain, use "automated circuit-breaking," "Identity Registry Storage (ERC-3643)," and "reduced manual review by 28%."

Focus: Describe these as "engines" or "architectures," not just tools you "know."

PARAGRAPH 3: THE STRATEGIC INTEGRATION
Mention one specific product or initiative the company is currently working on.
Explain exactly how your stack (e.g., ZK-notarization, agentic workflows, or ServiceNow IRM) accelerates that specific product's time-to-market or security posture.

STRICT NEGATIVE CONSTRAINTS (The "Kill List"):
No Identity Statements: Never start with "My name is..." or "I am a professional with..."
No "Empowered Builder" Label: Embody the actions of a builder (constructing systems), but do not use the specific phrase "Empowered Builder" to keep the tone corporate-appropriate.
No Citation Tags: Do not use [1], (Source), or any robotic reference markers.
No Flattery: Do not tell them they are "industry leaders" or "innovative." Show you know their work by discussing their tech.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: 
Regards,
Justin Gramke MBA CAMS

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with:
Regards,
Justin Gramke MBA CAMS"""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client()
    cl_prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=4096, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode)
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    return letter  # last attempt even if failed


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 6, limit: int = 20,
                       validation_mode: str = "normal", workers: int = 5) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        workers:         Number of parallel threads for generation.

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    base_resumes = profile.get("base_resumes", {})

    # Fallback to resume.txt only if no base_resumes are defined in profile
    default_resume_text = ""
    if not base_resumes and RESUME_PATH.exists():
        default_resume_text = RESUME_PATH.read_text(encoding="utf-8")

    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet
    jobs_rows = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY fit_score DESC LIMIT ?",
        (min_score, MAX_ATTEMPTS, limit),
    ).fetchall()

    if not jobs_rows:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    columns = jobs_rows[0].keys()
    jobs = [dict(zip(columns, row)) for row in jobs_rows]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Generating cover letters for %d jobs using %d workers (score >= %d)...", len(jobs), workers, min_score)
    t0 = time.time()
    completed = 0
    saved_count = 0
    error_count = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process_job(job):
        """Worker task: generate cover letter and return result."""
        try:
            # Select the correct resume persona for this job
            base_resume_key = job.get("base_resume_key")
            if base_resumes and base_resume_key and base_resume_key in base_resumes:
                resume_text = base_resumes[base_resume_key]
            elif base_resumes:
                resume_text = next(iter(base_resumes.values()))
            else:
                resume_text = default_resume_text

            letter = generate_cover_letter(resume_text, job, profile,
                                          validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF
            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path))
            except Exception:
                pass

            return {
                "url": job["url"],
                "path": str(cl_path),
                "pdf_path": pdf_path,
                "title": job["title"],
            }
        except Exception as e:
            log.error("Cover letter failed for %s: %s", job["title"], e)
            return {"url": job["url"], "title": job["title"], "error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_job = {executor.submit(_process_job, job): job for job in jobs}
        
        for future in as_completed(future_to_job):
            try:
                res = future.result()
                completed += 1
                
                # Update DB immediately
                now = datetime.now(timezone.utc).isoformat()
                if res.get("path"):
                    conn.execute(
                        "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                        "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                        (res["path"], now, res["url"]),
                    )
                    saved_count += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                        (res["url"],),
                    )
                    error_count += 1
                conn.commit()

                elapsed_loop = time.time() - t0
                rate = completed / elapsed_loop if elapsed_loop > 0 else 0
                log.info(
                    "%d/%d [DONE] | %.1f jobs/min | %s",
                    completed, len(jobs), rate * 60, res["title"][:40],
                )
            except Exception as e:
                log.error("Future processing failed: %s", e)
                error_count += 1

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved_count, error_count)

    return {
        "generated": saved_count,
        "errors": error_count,
        "elapsed": elapsed,
    }
