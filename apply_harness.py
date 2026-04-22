import asyncio
import logging
import os
import sys
import importlib
from pathlib import Path

# Try to import browser-use/harness components
try:
    from browser_use import Agent, Controller, Browser, ChatGoogle
except ImportError:
    # Fallbacks or dummy implementations if the user hasn't installed them yet
    pass

from applypilot import config
from applypilot.apply.dashboard import update_state, add_event
import helpers

logger = logging.getLogger(__name__)

# Initialize controller for custom actions
try:
    controller = Controller()

    # Register core helpers
    @controller.action('Pause for human review before final submission')
    async def pause_for_review(job_title: str, company: str):
        return await helpers.pause_for_review(job_title, company)
except NameError:
    # Fallback if browser-use is not installed
    controller = None

def load_domain_skills():
    """Dynamically load any skills from the domain-skills directory."""
    skills_dir = Path(__file__).parent / "domain-skills"
    if not skills_dir.exists():
        return
        
    for py_file in skills_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
            
        module_name = f"domain-skills.{py_file.stem}"
        try:
            importlib.import_module(module_name)
            logger.info(f"Loaded domain skill: {module_name}")
        except Exception as e:
            logger.error(f"Failed to load skill {module_name}: {e}")

# Call it once at startup
load_domain_skills()

async def apply_to_job(job: dict, tailored_resume: str, prompt_text: str, port: int, worker_id: int, pause_for_approval: bool = True) -> tuple[str, int]:
    """
    Main entry point for the new Browser-Harness apply path.
    Replaces GeminiBrowserDriver.run().
    """
    import time
    start_time = time.time()
    
    target_url = job.get("application_url") or job.get("url")
    job_title = job.get("title", "Unknown Role")
    company = job.get("site", "Unknown Company")
    
    update_state(worker_id, last_action="Starting Browser-Harness")
    add_event(f"[W{worker_id}] Launching Browser-Harness agent for {job_title}")
    
    try:
        # We assume the model is passed via environment or default to Flash/Pro
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return "failed:missing_api_key", int((time.time() - start_time) * 1000)
            
        llm = ChatGoogle(
            model=os.environ.get("APPLY_MODEL", "gemini-2.5-flash"), # Use a capable model
            api_key=api_key
        )
        
        # Connect to the existing Chrome instance launched by launcher.py
        # Playwright connects via cdp_url
        cdp_url = f"http://127.0.0.1:{port}"
        
        # Configure Browser-Harness to connect to the persistent instance
        browser = Browser(cdp_url=cdp_url)
        
        # Construct the task for the LLM
        task = (
            f"You are navigating to a job application for '{job_title}' at '{company}'.\n"
            f"The URL is: {target_url}\n"
            "Your objective is to completely fill out the job application using the provided applicant details.\n"
            "CRITICAL RULES:\n"
            "1. Map the applicant data to every required field.\n"
            "2. If you need to upload a resume, use the provided file upload helper.\n"
            "3. If you encounter a new, complex layout (like a weird tagger or login wall), you can write a helper function.\n"
            "4. WHEN THE FORM IS 100% COMPLETE and you are on the final review page, YOU MUST PAUSE.\n"
            "5. DO NOT CLICK THE FINAL SUBMIT BUTTON YOURSELF. Call the 'Pause for human review before final submission' action.\n\n"
            f"Applicant Data / Resume:\n{tailored_resume}\n\n"
            f"Prompt Guidance:\n{prompt_text}"
        )
        
        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            controller=controller,
        )
        
        # Run the agent
        history = await agent.run()
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        # In a full implementation, we'd inspect history.final_result() to determine success/fail
        if history and len(history.history) > 0:
             final_res = history.final_result() or ""
             update_state(worker_id, last_action="Agent finished")
             
             if "FAILED" in final_res.upper():
                 return f"failed:{final_res.split(':', 1)[-1][:50]}", duration_ms
             
             if pause_for_approval:
                 # In a real integration, we'd trigger the web GUI prompt here similar to _wait_for_user_approval
                 # For the terminal script, we just return applied as the agent paused successfully
                 return "applied", duration_ms
             
             return "applied", duration_ms
        else:
             return "failed:agent_error", duration_ms
             
    except Exception as e:
        logger.error(f"Browser-Harness failed: {e}", exc_info=True)
        duration_ms = int((time.time() - start_time) * 1000)
        return f"failed:{str(e)[:50]}", duration_ms
    finally:
        # DO NOT close the browser context here, as launcher.py manages it and we want to leave the tab open for review
        pass
