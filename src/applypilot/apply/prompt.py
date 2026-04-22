"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells the Gemini AI agent
how to fill out a job application form using browser actions. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt."""
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt."""
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK ==
Verify the job location before filling the form:
- Remote -> ELIGIBLE.
- Hybrid/Onsite in {city_list} -> ELIGIBLE.
- If the job is in a location NOT in your eligible list (e.g. Poland, Mexico, India, etc.) and it is NOT clearly marked as "Remote (USA)" or "Remote (Global)", you must fail with RESULT: FAILED:not_eligible_location.
- If the job explicitly requires Hybrid or Onsite presence in a city you are not in (e.g., onsite in Manila), fail.
- If unsure but it's clearly overseas, fail."""


def _build_salary_section(profile: dict) -> str:
    """Build salary negotiation instructions."""
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)

    return f"""== SALARY ==
Floor: ${floor} {currency}.
- If range posted (e.g. $120K-$160K), use MIDPOINT ($140K).
- If no range, use ${floor} {currency}.
- If range asked, use ${range_min}-${range_max} {currency}."""


def _build_screening_section(profile: dict) -> str:
    """Build screening questions guidance."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    work_auth = profile.get("work_authorization", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))

    auth_status = "Legally authorized: YES. Sponsorship required: NO."
    if work_auth.get("require_sponsorship") == "Yes":
        auth_status = "Legally authorized: NO (requires sponsorship)."

    return f"""== SCREENING QUESTIONS ==
- Location: lives in {city}, cannot relocate.
- Authorization: {auth_status}
- Skills: Candidate is a {target_role} with {years} years experience. Answer YES to relevant skill counts.
- Open-ended: Write 2-3 specific sentences based on the RESUME TEXT provided below."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section."""
    personal = profile["personal"]
    full_name = personal["full_name"]
    return f"""== HARD RULES ==
1. Never lie about citizenship or work authorization.
2. Use legal name: {full_name}."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False,
                 pause_for_approval: bool = False) -> str:
    """Build the full instruction prompt for the Gemini apply agent."""
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # Resume PDF path
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    
    # Avoid SameFileError if paths are identical
    if src_pdf.resolve() != upload_pdf.resolve():
        shutil.copy(str(src_pdf), str(upload_pdf))
    
    pdf_path = str(upload_pdf)

    # Prompt construction
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)

    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit button. Review only."
    else:
        submit_instruction = "Click Submit only after all fields are verified."

    prompt = f"""You are an autonomous Gemini-powered job application driver.
Your mission: Fill the application for {job['title']} at {job.get('site', 'Unknown')}.

== APPLICANT PROFILE ==
{profile_summary}

== RESUME TEXT (Use for all detail fields) ==
{tailored_resume}

== RESUME FILE (Upload this path) ==
{pdf_path}

{hard_rules}
{location_check}
{salary_section}
{screening_section}

== COVER LETTER TEXT (Paste into 'Cover Letter' or 'Additional Info' fields) ==
{cover_letter if cover_letter else "None provided."}

== MISSION STEPS ==
1. Navigate to the job URL.
2. Check location eligibility.
3. Fill all fields (Personal, Education, Experience) using the IDs provided in the element list.
4. Upload the Resume PDF at: {pdf_path}
5. Answer screening questions strategically based on the Resume Text.
6. DEAD-END DETECTION: If you have scrolled down 3 times and still see no interactive fields, no 'Apply' button, or are obviously on a non-job site, output RESULT:FAILED:no_form_found immediately.
7. {submit_instruction}
8. Output RESULT:APPLIED on success.

Available Actions:
ACTION: click(ID or "Label")
ACTION: type(ID or "Label", "Value")
ACTION: select(ID or "Label", "Option")
ACTION: upload(ID or "Label", "{pdf_path}")
ACTION: wait(seconds)
ACTION: scroll("down")
ACTION: navigate("url")

RESULT:APPLIED
RESULT:FAILED:reason
"""
    return prompt
