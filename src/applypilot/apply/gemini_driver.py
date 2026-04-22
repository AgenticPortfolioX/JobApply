import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image
from playwright.sync_api import sync_playwright

from applypilot.apply.dashboard import update_state, add_event
from applypilot import config

logger = logging.getLogger(__name__)

# Action regex for parsing Gemini responses
ACTION_RE = re.compile(r"ACTION:\s*(\w+)\s*\((.*)\)", re.IGNORECASE)
RESULT_RE = re.compile(r"RESULT:\s*(\w+[:\-_]?.*)", re.IGNORECASE)

class GeminiBrowserDriver:
    def __init__(self, worker_id: int, port: int = 9222, pause_for_approval: bool = False):
        self.worker_id = worker_id
        self.port = port
        self.pause_for_approval = pause_for_approval
        self.log_file = config.LOG_DIR / f"worker-{worker_id}.log"
        self._last_actions = [] # Buffer for repetition detection
        self._last_urls = []    # Buffer for stall detection
        self._url_turns = 0     # Turns on current URL
        
        # Load key from .env relative to the package root (up one level from ApplyPilot-Custom)
        config.load_env()
        self.api_key = os.environ.get("GEMINI_API_KEY")
        
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment or .env file.")
        
        self.client = genai.Client(api_key=self.api_key)
        # Standardized model ID for google-genai SDK
        self.model_id = os.environ.get("APPLY_MODEL", "gemini-2.5-flash")

    def _get_screenshot_bytes(self):
        """Take a screenshot and return as bytes."""
        return self.page.screenshot(type="png", full_page=False)

    def _get_page_context(self):
        """Get the current page URL and a simplified representation with numeric IDs."""
        url = self.page.url
        try:
            # Inject labels and capture simplified Context
            dom_data = self.page.evaluate('''() => {
                const interactables = document.querySelectorAll('button, input, select, textarea, a, [role="button"]');
                const labels = [];
                interactables.forEach((el, index) => {
                    el.setAttribute('data-applypilot-id', index);
                    let label = el.innerText || el.placeholder || el.name || el.ariaLabel || el.textContent || '';
                    labels.push(`ID [${index}]: ${el.tagName} "${label.substring(0, 50).trim()}"`);
                });
                return {
                    text: document.body.innerText.substring(0, 15000),
                    elements: labels.join('\n')
                };
            }''')
            content = dom_data['text']
            elements = dom_data['elements']
        except Exception:
            content = "Could not extract text content."
            elements = "None"
        
        return f"URL: {url}\nVisible Text Content:\n{content}\n\nInteractive Elements:\n{elements}"

    def _log(self, msg: str):
        """Log message to both Python logger and the worker log file."""
        logger.info(f"[W{self.worker_id}] {msg}")
        # Update dashboard state
        update_state(self.worker_id, last_action=msg[:35])
        add_event(f"[W{self.worker_id}] {msg}")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"  >> {msg}\n")

    def run(self, job: dict, tailored_resume: str, prompt_text: str, timeout_mins: int = 15):
        """Main execution loop for a single job application."""
        start_time = time.time()
        max_turns = 40
        turn = 0
        history = []
        
        with sync_playwright() as p:
            try:
                # Connect to the Chrome instance launched by launcher.py
                self.browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
                self.context = self.browser.contexts[0]
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                
                # Force navigation to target if not yet there
                target_url = job.get("application_url") or job.get("url")
                if target_url and target_url not in self.page.url:
                    try:
                        self._log(f"Navigating to {target_url}")
                        self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        self._log(f"Initial navigation failed: {e}")

                while turn < max_turns:
                    turn += 1
                    update_state(self.worker_id, actions=turn, last_action=f"Turn {turn}")
                    self._log(f"Turn {turn}...")
                    
                    elapsed = time.time() - start_time
                    if elapsed > timeout_mins * 60:
                        return "failed:timeout", int(elapsed * 1000)

                    # 1. Capture State
                    capture_success = False
                    for _ in range(3):
                        try:
                            # Filter closed pages
                            open_pages = [p for p in self.context.pages if not p.is_closed()]
                            if not open_pages:
                                self._log("All pages closed. Browser might have crashed.")
                                return "failed:browser_closed", int((time.time() - start_time) * 1000)
                                
                            self.page = open_pages[-1]
                            self.page.bring_to_front()
                            
                            screenshot_bytes = self._get_screenshot_bytes()
                            page_info = self._get_page_context()
                            capture_success = True
                            break
                        except Exception as e:
                            self._log(f"State capture retry: {e}")
                            time.sleep(2)
                            
                    if not capture_success:
                        self._log("Fatal: Snapshot failed continuously. Ending application.")
                        return "failed:capture_error", int((time.time() - start_time) * 1000)
                        
                    # 2. Build Request using New SDK
                    
                    gemini_override = (
                        "CRITICAL: You are the VISION-ACTION DRIVER for a job application.\n"
                        "Your job: Fill in ALL form fields using the applicant profile below, then stop on the FINAL SUBMIT button.\n"
                        "The user (human) will click the final Submit/Apply button themselves.\n"
                        "DO NOT click the final 'Submit Application' or 'Apply' button — stop just before it.\n"
                        "DO NOT attempt to call functions. DO NOT output JSON. Output EXACTLY ONE plain-text action per turn.\n\n"
                        "Available Actions:\n"
                        'ACTION: click(ID or "Button Text")\n'
                        'ACTION: type(ID or "Label", "value")\n'
                        'ACTION: select(ID or "Label", "option")\n'
                        'ACTION: upload(ID or "Button", "path/to/file.pdf")\n'
                        'ACTION: wait(3)\n'
                        'ACTION: scroll("down")\n'
                        'ACTION: navigate("https://example.com")\n'
                        'RESULT: APPLIED\n'
                        'RESULT: FAILED:reason\n\n'
                        "Form Filling Rules:\n"
                        "1. If a field label has trailing * or , just ignore punctuation when referencing it.\n"
                        "2. If a click fails, try scrolling to find the element, then retry.\n"
                        "3. If a page requires login, output RESULT: FAILED:login_issue\n"
                        "4. If the job is not in an eligible location, output RESULT: FAILED:not_eligible_location\n"
                        "5. When ALL fields are filled and you are on the final review/submit page, output RESULT: APPLIED\n\n"
                        f"ACTION HISTORY (Last 5 turns):\n{chr(10).join(history[-5:]) if history else 'None'}\n\n"
                        f"APPLICANT CONTEXT & INSTRUCTIONS:\n{prompt_text}\n\n"
                        f"CURRENT PAGE INFO:\n{page_info}\n\nTurn: {turn}/40"
                    )

                    contents = [
                        types.Content(
                            role="user",
                            parts=[
                                types.Part(text=gemini_override),
                                types.Part(inline_data=types.Blob(mime_type="image/png", data=screenshot_bytes))
                            ]
                        )
                    ]
                    
                    # 3. Ask Gemini (Explicitly disable AFC)
                    config = types.GenerateContentConfig(
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        )
                    )
                    
                    response = self.client.models.generate_content(
                        model=self.model_id,
                        contents=contents,
                        config=config
                    )
                    
                    # 4. Safely get text
                    response_text = ""
                    if response and response.candidates:
                        candidate = response.candidates[0]
                        # Handle safety filters or blocked content
                        if candidate.finish_reason != "STOP" and candidate.finish_reason is not None:
                            self._log(f"Gemini generation not finished normally. Reason: {candidate.finish_reason}")
                        
                        if candidate.content and candidate.content.parts and candidate.content.parts[0].text:
                            response_text = candidate.content.parts[0].text
                        else:
                            self._log(f"No text content in candidate part. Raw: {candidate.content}")
                            self._log("Gemini returned an empty response or was blocked by safety filters.")
                            time.sleep(3)
                            continue

                    # 5. Repetition & Stall Detection
                    current_url = self.page.url
                    if not self._last_urls or self._last_urls[-1] == current_url:
                        self._url_turns += 1
                    else:
                        self._url_turns = 1
                    self._last_urls.append(current_url)
                    if len(self._last_urls) > 10:
                        self._last_urls.pop(0)

                    action_clean = response_text.strip()
                    
                    # 5a. Identical repetition
                    if len(self._last_actions) >= 3 and all(a == action_clean for a in self._last_actions[-3:]):
                        if "scroll" in action_clean.lower():
                             self._log("Repetitive scrolling detected. Ending as dead-end.")
                             return "failed:no_form_found", int((time.time() - start_time) * 1000)
                        else:
                             self._log(f"Repetitive action '{action_clean[:30]}' detected. Ending loop.")
                             return "failed:repetition_detected", int((time.time() - start_time) * 1000)

                    # 5b. URL Stall (Staying on same URL for too long)
                    if self._url_turns > 15:
                        self._log("Stuck on same URL for 15 turns. Ending as dead-end.")
                        return "failed:dead_end_url", int((time.time() - start_time) * 1000)

                    self._last_actions.append(action_clean)
                    if len(self._last_actions) > 5:
                        self._last_actions.pop(0)

                    # 6. Parse Result/Action
                    result_match = RESULT_RE.search(response_text)
                    if result_match:
                        result_code = result_match.group(1).strip().lower()
                        duration_ms = int((time.time() - start_time) * 1000)
                        self._log(f"Result: {result_code}")
                        if result_code == "applied" and self.pause_for_approval:
                            return self._wait_for_user_approval("FINISH"), duration_ms
                        return result_code, duration_ms

                    action_match = ACTION_RE.search(response_text)
                    if not action_match:
                        self._log("Waiting (no action found in vision)")
                        time.sleep(3)
                        continue

                    action_name = action_match.group(1).lower()
                    action_args_raw = action_match.group(2)
                    
                    # Simple arg parser (handles quoted strings)
                    # This allows parsing 'click("Submit")' into ['Submit']
                    import shlex
                    try:
                        args = shlex.split(action_args_raw.replace('"', "'"))
                        # Remove trailing/leading quotes if any
                        args = [a.strip("'") for a in args]
                    except:
                        args = [action_args_raw.strip('"\'')]

                    if not args and action_args_raw.isdigit():
                         args = [int(action_args_raw)]

                    # 5. Execute Action
                    try:
                        self._log(f"{action_name} {args}")
                        history.append(f"Turn {turn}: {action_name}({args})")
                        self._execute_action(action_name, args)
                    except Exception as e:
                        self._log(f"Action failed: {e}")
                        history.append(f"Turn {turn}: FAILED to {action_name} - {str(e)}")
                        time.sleep(2)

            except Exception as e:
                self._log(f"Fatal error in Gemini driver: {e}")
                import traceback
                self._log(traceback.format_exc())
                return f"failed:{str(e)}", int((time.time() - start_time) * 1000)
            finally:
                if self.browser:
                    self.browser.close()

    def _wait_for_user_approval(self, reason: str = "FINISH"):
        """Pause execution and wait for user input from stdin (Web GUI sends this)."""
        msg = f"ACTION_REQUIRED:PENDING_APPROVAL:{self.worker_id}:{self.port}:{reason}"
        self._log(f"*** {msg} ***")
        self._log("Type 'y' to mark as APPLIED or 'n' to mark as FAILED (skipped).")
        
        import sys
        update_state(self.worker_id, status="waiting", last_action=f"Paused: {reason}")
        
        try:
            line = sys.stdin.readline()
            if not line:
                return "failed:aborted"
            choice = line.strip().lower()
            if choice == 'y':
                return "applied"
            else:
                return "failed:user_rejected"
        except Exception as e:
            self._log(f"Approval wait error: {e}")
            return "failed:error_during_wait"

    def _execute_action(self, name, args):
        """Map action name to Playwright commands with multi-strategy fallback."""
        
        if name == "click":
            selector = args[0]
            self._robust_click(selector)
        
        elif name == "type":
            if len(args) < 2:
                self._log(f"type action needs 2 args, got: {args}")
                return
            selector, text = args[0], args[1]
            self._robust_fill(selector, text)
        
        elif name == "select":
            # Handles <select> dropdowns
            if len(args) < 2:
                return
            selector, value = args[0], args[1]
            try:
                el = self._find_element(selector)
                if el:
                    el.select_option(value=value)
                else:
                    self.page.select_option(selector, value=value)
            except Exception:
                try:
                    self.page.get_by_label(selector.strip("*,:")).select_option(value=value)
                except Exception as e2:
                    raise e2
        
        elif name == "navigate":
            self.page.goto(args[0], wait_until="domcontentloaded", timeout=30000)
            
        elif name == "upload":
            selector = args[0]
            path = args[1] if len(args) > 1 else args[0]
            with self.page.expect_file_chooser(timeout=15000) as fc_info:
                self._robust_click(selector)
            file_chooser = fc_info.value
            file_chooser.set_files(path)
            
        elif name == "wait":
            seconds = int(args[0]) if args else 3
            time.sleep(min(seconds, 10))
        
        elif name == "scroll":
            direction = args[0].lower() if args else "down"
            if direction == "down":
                self.page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            else:
                self.page.evaluate("window.scrollBy(0, -window.innerHeight * 0.8)")
            time.sleep(0.5)
            return  # skip load-wait for scrolls
        
        try:
            self.page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        time.sleep(0.8)

    def _find_element(self, selector: str):
        """Try to find an element by CSS selector or label matching."""
        # If it looks like a CSS selector, use it directly
        if selector.startswith((".", "#", "[", "input", "button", "textarea", "select")):
            try:
                return self.page.locator(selector).first
            except Exception:
                return None
        return None

    def _robust_fill(self, label_or_selector: str, text: str):
        """Try multiple strategies to fill a form field."""
        # Clean label - strip trailing punctuation that Gemini often includes
        clean_label = str(label_or_selector).strip(" *:,")
        
        strategies = [
            # 1. Numeric ID from ApplyPilot labeling
            lambda: self.page.locator(f'[data-applypilot-id="{clean_label}"]').first.fill(text) 
                    if clean_label.isdigit() else (_ for _ in ()).throw(Exception()),
            # 2. Direct CSS selector
            lambda: self.page.locator(label_or_selector).first.fill(text) 
                    if label_or_selector.startswith((".", "#", "[")) else (_ for _ in ()).throw(Exception()),
            # 3. Exact label match
            lambda: self.page.get_by_label(clean_label, exact=True).first.fill(text),
            # 4. Fuzzy label match
            lambda: self.page.get_by_label(clean_label, exact=False).first.fill(text),
            # 5. Placeholder match
            lambda: self.page.get_by_placeholder(clean_label, exact=False).first.fill(text),
        ]
        
        last_err = None
        for fn in strategies:
            try:
                fn()
                return
            except Exception as e:
                last_err = e
                continue
        
        raise Exception(f"Could not fill '{label_or_selector}': {last_err}")

    def _robust_click(self, label_or_selector: str):
        """Try multiple strategies to click an element."""
        clean_label = str(label_or_selector).strip(" *:,")
        
        strategies = [
            # 1. Numeric ID from ApplyPilot labeling
            lambda: self.page.locator(f'[data-applypilot-id="{clean_label}"]').first.click(timeout=10000)
                    if clean_label.isdigit() else (_ for _ in ()).throw(Exception()),
            # 2. CSS selector
            lambda: self.page.locator(label_or_selector).first.click(timeout=10000)
                    if label_or_selector.startswith((".", "#", "[")) else (_ for _ in ()).throw(Exception()),
            # 3. Exact text
            lambda: self.page.get_by_text(clean_label, exact=True).first.click(timeout=10000),
            # 4. Fuzzy text
            lambda: self.page.get_by_text(clean_label, exact=False).first.click(timeout=10000),
            # 5. Button role
            lambda: self.page.get_by_role("button", name=clean_label).first.click(timeout=10000),
            # 6. Link role
            lambda: self.page.get_by_role("link", name=clean_label).first.click(timeout=10000),
        ]
        
        last_err = None
        for fn in strategies:
            try:
                fn()
                return
            except Exception as e:
                last_err = e
                continue
        
        raise Exception(f"Could not click '{label_or_selector}': {last_err}")


def run_gemini_apply(worker_id, job, tailored_resume, prompt_text, port, pause_for_approval=False):
    """Entry point for the launcher. Now delegates to the Browser-Harness engine."""
    import asyncio
    from apply_harness import apply_to_job
    
    # Run the new async apply_to_job function in a synchronous context
    # This allows launcher.py to remain unchanged in its calling signature
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    return loop.run_until_complete(
        apply_to_job(
            job=job,
            tailored_resume=tailored_resume,
            prompt_text=prompt_text,
            port=port,
            worker_id=worker_id,
            pause_for_approval=pause_for_approval
        )
    )
