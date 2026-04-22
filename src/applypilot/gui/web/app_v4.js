// Navigation Logic
document.querySelectorAll('.nav-links li').forEach(link => {
    link.addEventListener('click', function(e) {
        const li = e.target.closest('li');
        if (!li) return;

        document.querySelectorAll('.nav-links li').forEach(l => l.classList.remove('active'));
        li.classList.add('active');

        const viewId = `view-${li.dataset.view}`;
        const viewEl = document.getElementById(viewId);
        if (viewEl) {
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            viewEl.classList.add('active');
        }

        if (li.dataset.view === 'dashboard') loadDashboard();
        if (li.dataset.view === 'review') loadReviewFiles();
        if (li.dataset.view === 'execution') loadQueue();
    });
});

// Real-Time Dashboard Fetch
async function loadDashboard() {
    try {
        const res = await fetch('/api/status');
        const stats = await res.json();
        
        // Update Sidebar Badges
        updateSidebarCounters(stats);

        const grid = document.getElementById('stats-grid');
        grid.innerHTML = `
            <div class="stat-card glass-panel">
                <span class="value">${stats.total || 0}</span>
                <span class="label">Jobs Discovered</span>
            </div>
            <div class="stat-card glass-panel" style="border-left: 2px solid var(--primary);">
                <span class="value">${stats.scored || 0}</span>
                <span class="label">AI Scored</span>
            </div>
            <div class="stat-card glass-panel" style="border-left: 2px solid var(--warning);">
                <span class="value">${stats.pending_detail || 0}</span>
                <span class="label">In Review</span>
            </div>
            <div class="stat-card glass-panel" style="border-left: 2px solid #3b82f6;">
                <span class="value">${stats.tailored || 0}</span>
                <span class="label">Ready to Apply</span>
            </div>
            <div class="stat-card glass-panel" style="border-left: 2px solid var(--success);">
                <span class="value">${stats.applied || 0}</span>
                <span class="label">Total Applied</span>
            </div>
        `;
    } catch (e) {
        console.error("Dashboard load failed", e);
    }
}

function updateSidebarCounters(stats) {
    document.getElementById('badge-total').textContent = stats.total || 0;
    document.getElementById('badge-review').textContent = stats.pending_detail || 0;
    document.getElementById('badge-ready').textContent = stats.tailored || 0;
}

async function resetStage(stage) {
    const stageNames = {
        'discovery': 'ALL Jobs and History',
        'scoring': 'AI Scores for all jobs',
        'tailoring': 'Execution Queue and Tailored Files'
    };
    
    if (!confirm(`CAUTION: Are you sure you want to clear ${stageNames[stage]}? This action cannot be undone.`)) return;
    
    appendTerminal(`\n[SYSTEM] Resetting stage: ${stage}...\n`);
    try {
        const res = await fetch(`/api/process/reset/${stage}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'reset') {
            appendTerminal(`[SUCCESS] Stage ${stage} cleared.\n`);
            loadDashboard();
            if (stage === 'scoring') loadReviewFiles();
            if (stage === 'tailoring') loadQueue();
        }
    } catch (e) {
        appendTerminal(`[ERROR] Reset failed.\n`);
    }
}

// Review State Loader
let currentPrefix = "";
async function loadReviewFiles() {
    try {
        const res = await fetch('/api/files/review');
        const jobs = await res.json();
        const list = document.getElementById('review-list');
        list.innerHTML = "";
        if (jobs.length === 0) {
            list.innerHTML = '<li style="color:var(--text-muted);cursor:default;">No pending reviews</li>';
            return;
        }
        jobs.forEach((job, index) => {
            const li = document.createElement('li');
            li.textContent = job.prefix.replace(/_/g, ' ');
            li.dataset.prefix = job.prefix;
            li.onclick = () => {
                document.querySelectorAll('#review-list li').forEach(el => el.classList.remove('active'));
                li.classList.add('active');
                selectReviewJob(job);
            };
            list.appendChild(li);

            // Auto-select the first job if none selected
            if (index === 0 && !window.currentReviewJob) {
                li.click();
            }
        });
    } catch(e) {}
}

async function selectReviewJob(job) {
    console.log("Selecting Review Job:", job);
    document.getElementById('rv-title').textContent = job.prefix.replace(/_/g, ' ');
    window.currentReviewJob = job;

    // Auto-load all 3 panes with styling
    if (job.job_file) {
        updateIframeContent('job-viewer', `/api/files/download/${encodeURIComponent(job.job_file)}`);
    } else {
        document.getElementById('job-viewer').srcdoc = '';
    }

    if (job.resume_file) {
        updateIframeContent('resume-viewer', `/api/files/download/${encodeURIComponent(job.resume_file)}`);
    } else {
        document.getElementById('resume-viewer').srcdoc = '';
    }

    if (job.cover_file) {
        updateIframeContent('cover-viewer', `/api/files/cover/${encodeURIComponent(job.cover_file)}`);
    } else {
        document.getElementById('cover-viewer').srcdoc = '';
    }
}

async function approveCurrentJob() {
    if (!window.currentReviewJob) return;
    const prefix = window.currentReviewJob.prefix;
    appendTerminal(`\n[SYSTEM] Approving job: ${prefix.replace(/_/g,' ')}...\n`);
    try {
        const res = await fetch(`/api/files/approve/${encodeURIComponent(prefix)}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'approved') {
            appendTerminal(`[SUCCESS] Files moved to Execution Hub.\n`);
            if (data.agent === 'launched') {
                appendTerminal(`[AGENT] Auto-Apply agent launching... Watch Chrome and confirm submission in the ACTION REQUIRED modal below.\n`);
            } else if (data.agent === 'skipped') {
                appendTerminal(`[WARN] Could not auto-launch agent: ${data.reason}. Stop the current process first, then use the Execution Hub.\n`);
            }
            loadReviewFiles();
            document.getElementById('job-viewer').src = 'about:blank';
            document.getElementById('resume-viewer').src = 'about:blank';
            document.getElementById('cover-viewer').src = 'about:blank';
            document.getElementById('rv-title').textContent = 'Select a Job';
            window.currentReviewJob = null;
            loadDashboard();
        } else {
            appendTerminal(`[ERROR] Approval failed: ${data.detail || 'Unknown error'}\n`);
        }
    } catch (e) {
        appendTerminal(`[ERROR] Network error during approval.\n`);
    }
}

async function rejectCurrentJob() {
    if (!window.currentReviewJob) return;
    if (!confirm('Are you sure you want to REJECT and DELETE these review files?')) return;
    const prefix = window.currentReviewJob.prefix;
    appendTerminal(`\n[SYSTEM] Rejecting job: ${prefix.replace(/_/g,' ')}...\n`);
    try {
        await fetch(`/api/files/reject/${encodeURIComponent(prefix)}`, { method: 'POST' });
        appendTerminal(`[SYSTEM] Files deleted.\n`);
        loadReviewFiles();
        document.getElementById('job-viewer').src = 'about:blank';
        document.getElementById('resume-viewer').src = 'about:blank';
        document.getElementById('cover-viewer').src = 'about:blank';
        document.getElementById('rv-title').textContent = 'Select a Job';
        window.currentReviewJob = null;
        loadDashboard();
    } catch (e) {}
}

// Queue Loader
async function loadQueue() {
    try {
        const res = await fetch('/api/queue');
        const queue = await res.json();
        const list = document.getElementById('execution-queue-list');
        list.innerHTML = "";
        if (queue.length === 0) {
            list.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);text-align:center;padding:30px;">No jobs ready yet. Approve jobs from Review State.</td></tr>';
            return;
        }
        window.executionJobs = queue;
        queue.forEach((job, index) => {
            const statusBadge = job.applied_at
                ? `<span style="color:var(--success);">✅ Applied</span>`
                : job.apply_status === 'failed'
                ? `<span style="color:var(--danger);">❌ Failed</span><div style="font-size:10px;color:rgba(255,180,171,0.7);margin-top:2px;">${escapeHtml(job.apply_error || 'unknown')}</div>`
                : `<span style="color:var(--warning);">⏳ Ready</span>`;
            const tr = document.createElement('tr');
            tr.style.cursor = 'pointer';
            tr.onclick = (e) => {
                // Prevent row click if clicking the Apply button
                if (e.target.tagName === 'BUTTON') return;
                document.querySelectorAll('#execution-queue-list tr').forEach(el => el.classList.remove('active'));
                tr.classList.add('active');
                selectExecutionJob(job);
            };
            tr.innerHTML = `
                <td><strong style="color: var(--success)">${job.fit_score}/10</strong></td>
                <td><div style="font-weight:bold;">${job.title}</div><div style="font-size:11px;color:#a0a8c0;">${job.site}</div></td>
                <td>${statusBadge}</td>
                <td>
                    <button class="btn btn-secondary" onclick="launchSingle('${job.url}')" style="padding: 6px 12px; font-size: 11px;">Apply</button>
                    <button class="btn btn-danger" onclick="discardJob('${job.url}')" style="padding: 6px 12px; font-size: 11px; margin-left: 5px;">Discard</button>
                </td>
            `;
            list.appendChild(tr);
        });
    } catch (e) {}
}

function selectExecutionJob(job) {
    document.getElementById('eh-title').textContent = `${job.title} @ ${job.site}`;
    loadExecutionDocuments(job);
}

function loadExecutionDocuments(job) {
    console.log("Loading Execution Documents for Job:", job);
    if (!job.tailored_resume_path) {
        console.warn("No tailored_resume_path found for job", job.url);
        document.getElementById('eh-resume-viewer').srcdoc = '';
        document.getElementById('eh-job-viewer').srcdoc = '';
        document.getElementById('eh-cover-viewer').srcdoc = '';
        return;
    }

    // Extract basename from tailored_resume_path
    const resumePath = job.tailored_resume_path.replace(/\\/g, '/');
    const resumeFilename = resumePath.split('/').pop();
    
    // Prefix is everything before the last extension
    const prefix = resumeFilename.replace(/\.[^/.]+$/, "");
    const jobFilename = `${prefix}_JOB.txt`;
    
    console.log("Resolved Filenames - Resume:", resumeFilename, "Job:", jobFilename);
    
    updateIframeContent('eh-resume-viewer', `/api/files/tailored/${encodeURIComponent(resumeFilename)}?t=${Date.now()}`);
    updateIframeContent('eh-job-viewer', `/api/files/tailored/${encodeURIComponent(jobFilename)}?t=${Date.now()}`);
    
    if (job.cover_letter_path) {
        const coverPath = job.cover_letter_path.replace(/\\/g, '/');
        const coverFilename = coverPath.split('/').pop();
        console.log("Resolved Cover Letter:", coverFilename);
        updateIframeContent('eh-cover-viewer', `/api/files/cover/${encodeURIComponent(coverFilename)}?t=${Date.now()}`);
    } else {
        console.log("No cover letter path found for job");
        document.getElementById('eh-cover-viewer').srcdoc = '';
    }
}

/**
 * Fetches text content and injects it into an iframe with a dark-mode wrapper.
 */
async function updateIframeContent(iframeId, url) {
    const iframe = document.getElementById(iframeId);
    if (!iframe) return;

    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error("File not found");
        
        const text = await res.text();
        console.log(`Fetched ${url}, size: ${text.length} chars`);
        
        if (text.trim().length === 0) {
            iframe.srcdoc = `<html><head><style>body{color:#f59e0b; font-family:sans-serif; padding:15px; background:#0b1326;}</style></head><body>[WARN] File is empty. The tailoring process may have failed or was interrupted. Try re-running the 'Tailor' stage.</body></html>`;
            return;
        }

        const html = `
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body {
                        background-color: transparent;
                        color: #dae2fd;
                        font-family: 'Inter', -apple-system, blinkmacsystemfont, 'Segoe UI', roboto, sans-serif;
                        font-size: 14px;
                        line-height: 1.6;
                        margin: 20px;
                        white-space: pre-wrap;
                        word-wrap: break-word;
                    }
                    /* Ensure scrollbars look okay in dark mode */
                    ::-webkit-scrollbar { width: 8px; }
                    ::-webkit-scrollbar-track { background: transparent; }
                    ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }
                    ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }
                </style>
            </head>
            <body>${escapeHtml(text)}</body>
            </html>
        `;
        iframe.srcdoc = html;
    } catch (e) {
        iframe.srcdoc = `<html><body style="color:#ffb4ab;font-family:sans-serif;padding:20px;">[ERROR] ${e.message}</body></html>`;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Processes & CLI Execution
const activeWorkers = new Map();

// ─── Auto-Pilot Sequential Queue State ───────────────────────────────────────
window.autoPilotRunning = false;
window.autoPilotQueue   = [];   // Array of job objects fetched from /api/queue
window.autoPilotIndex   = 0;    // Current position in the queue

function _stablePid(url) {
    // Derive a short, filesystem-safe process ID from a job URL
    // e.g. "apply_aHR0cHM6Ly93d3cua" (URL base64 prefix)
    try {
        return 'apply_' + btoa(url).replace(/[^a-zA-Z0-9]/g, '').slice(0, 24);
    } catch (e) {
        return 'apply_' + Math.random().toString(36).slice(2, 14);
    }
}

async function toggleAutoPilot() {
    if (window.autoPilotRunning) {
        stopAutoPilot();
    } else {
        await startAutoPilot();
    }
}

async function startAutoPilot() {
    if (window.autoPilotRunning) return;

    // Fetch the current queue directly so we have an ordered snapshot
    let queue;
    try {
        const res = await fetch('/api/queue');
        queue = await res.json();
    } catch (e) {
        appendTerminal(`\n[ERROR] Could not fetch job queue.\n`);
        return;
    }

    // Only include truly pending jobs (no applied_at, no failed status)
    const pending = queue.filter(j => !j.applied_at && j.apply_status !== 'failed' && j.apply_status !== 'in_progress');
    if (pending.length === 0) {
        appendTerminal(`\n[AUTO-PILOT] No pending jobs in queue. Nothing to launch.\n`);
        return;
    }

    window.autoPilotRunning = true;
    window.autoPilotQueue   = pending;
    window.autoPilotIndex   = 0;

    const btn = document.getElementById('btn-autopilot');
    if (btn) {
        btn.textContent = '⏹ Stop Auto-Pilot';
        btn.classList.remove('btn-play');
        btn.classList.add('btn-danger');
    }

    appendTerminal(`\n[AUTO-PILOT] Starting sequential queue (${pending.length} jobs)...\n`);
    _launchNextInQueue();
}

function stopAutoPilot() {
    window.autoPilotRunning = false;
    window.autoPilotQueue   = [];
    window.autoPilotIndex   = 0;

    const btn = document.getElementById('btn-autopilot');
    if (btn) {
        btn.textContent = '🚀 Launch Auto-Pilot';
        btn.classList.remove('btn-danger');
        btn.classList.add('btn-play');
    }

    appendTerminal(`\n[AUTO-PILOT] Stopped by user.\n`);
}

function _launchNextInQueue() {
    if (!window.autoPilotRunning) return;

    const queue = window.autoPilotQueue;
    const idx   = window.autoPilotIndex;

    if (idx >= queue.length) {
        appendTerminal(`\n[AUTO-PILOT] ✅ All ${queue.length} jobs processed. Auto-Pilot complete.\n`);
        stopAutoPilot();
        loadQueue();
        loadDashboard();
        return;
    }

    const job = queue[idx];
    const pid = _stablePid(job.url);

    appendTerminal(`\n[AUTO-PILOT] Job ${idx + 1}/${queue.length}: ${job.title} @ ${job.site} (${pid})\n`);
    window.autoPilotIndex = idx + 1;

    // Store current pid so the exit handler knows which queue to advance
    window.autoPilotCurrentPid = pid;

    launchSingleWithPid(job.url, pid);
}


async function stop_all_processes() {
    appendTerminal(`\n[SYSTEM] Killing all active bots...\n`);
    await fetch('/api/process/stop_all', { method: 'POST' });
    activeWorkers.clear();
    updateWorkerGrid();
    // Also stop auto-pilot if it's running
    if (window.autoPilotRunning) stopAutoPilot();
}


async function launchSync() {
    appendTerminal(`\n\n[SYSTEM] Synchronizing Daily Job Tracker (Google/TheirStack/Adzuna)...\n`);
    try {
        const res = await fetch('/api/process/launch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: 'sync', args: [] })
        });
        const data = await res.json();
        if (data.status === 'started') {
            appendTerminal(`[SUCCESS] Sync process started.\n`);
        } else {
            appendTerminal(`[ERROR] Failed to start sync: ${data.detail || 'Unknown error'}\n`);
        }
    } catch (e) {
        appendTerminal(`[ERROR] Network error during sync launch.\n`);
    }
}

async function launchDiscovery() {
    appendTerminal(`\n\n[SYSTEM] Launching Stage 1: Discovery...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'run', args: ['discover', 'enrich'] })
    });
}

async function launchScoring() {
    appendTerminal(`\n\n[SYSTEM] Launching Stage 2: AI Scoring...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'run', args: ['score'] })
    });
}

async function launchTailoring() {
    appendTerminal(`\n\n[SYSTEM] Launching Stage 3: Tailoring & PDF Generation...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'run', args: ['tailor', 'cover', 'pdf'] })
    });
}

async function launchAll() {
    appendTerminal(`\n\n[SYSTEM] Launching FULL Pipeline (Stages 1-5)...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'run', args: ['all'] })
    });
}

async function launchPilotQueue() {
    appendTerminal(`\n\n[SYSTEM] Launching Stage 6 ApplyPilot Queue (Batch Mode: 2 Parallel Workers)...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'apply', args: ['--limit', '0', '--workers', '2', '--pause-for-approval', '--model', 'gemini-2.5-flash'] })
    });
}

async function discardJob(url) {
    if (!confirm("Are you sure you want to discard this tailored resume? This will delete the files.")) return;
    
    appendTerminal(`\n[SYSTEM] Discarding job from queue...\n`);
    try {
        const res = await fetch('/api/queue/discard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url })
        });
        const data = await res.json();
        if (data.status === 'discarded') {
            appendTerminal(`[SUCCESS] Job discarded and files deleted.\n`);
            loadQueue();
            loadDashboard();
        }
    } catch (e) {
        appendTerminal(`[ERROR] Failed to discard job.\n`);
    }
}

async function launchSingle(url) {
    const pid = _stablePid(url);
    await launchSingleWithPid(url, pid);
}

async function launchSingleWithPid(url, pid) {
    appendTerminal(`\n\n[SYSTEM] Launching Pilot for: ${url.slice(0, 60)}... (${pid})\n`);
    try {
        const res = await fetch('/api/process/launch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                command: 'apply',
                args: ['--url', url, '--pause-for-approval'],
                process_id: pid   // stable ID — fixes Bug 2 & 3
            })
        });
        const data = await res.json();
        if (res.ok && data.status === 'started') {
            appendTerminal(`[SYSTEM] Agent started as "${data.process_id}". Watch Chrome.\n`);
        } else {
            const msg = data.detail || JSON.stringify(data);
            appendTerminal(`[WARN] ${msg}\n`);
            // If already running and we're in auto-pilot, advance past this job
            if (window.autoPilotRunning && msg.includes('already running')) {
                appendTerminal(`[AUTO-PILOT] Skipping (already active): advancing queue.\n`);
                setTimeout(_launchNextInQueue, 2000);
            }
        }
    } catch (e) {
        appendTerminal(`[ERROR] Network error launching agent: ${e.message}\n`);
        if (window.autoPilotRunning) {
            appendTerminal(`[AUTO-PILOT] Error — advancing to next job.\n`);
            setTimeout(_launchNextInQueue, 3000);
        }
    }
}

async function stopProcess(pid) {
    await fetch(`/api/process/stop/${pid}`, { method: 'POST' });
}

// WebSocket for Terminal Streaming
const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${wsProtocol}//${window.location.host}/ws/terminal`;
let ws = new WebSocket(wsUrl);

ws.onmessage = function(event) {
    const msg = JSON.parse(event.data);
    const pid = msg.process_id || "default";

    if (msg.type === "log") {
        appendTerminal(`[${pid}] ${msg.data}`);
        updateWorkerState(pid, msg.data);
    } else if (msg.type === "APPROVAL_REQUEST") {
        window.activeApprovalPid = pid;
        const workerId = msg.worker_id !== undefined ? `Worker ${msg.worker_id}` : pid;
        const reason = msg.reason || "Confirmation";
        
        // Display worker-specific details in the modal
        const reasonLabel = reason === 'FINISH' ? 'Ready to Submit' : 'Action Failed / Manual Intervention';
        const reasonColor = reason === 'FINISH' ? 'var(--success)' : 'var(--danger)';
        
        document.getElementById('approval-job-name').innerHTML = `
            <div style="font-size:24px; color:var(--primary); font-weight:bold;">${workerId}</div>
            <div style="font-size:16px; margin-top:10px; color:var(--text);">
                Status: <span style="color:${reasonColor}; font-weight:bold;">${reasonLabel}</span>
            </div>
        `;
        document.getElementById('approval-overlay').classList.remove('hidden');
    } else if (msg.type === "exit") {
        appendTerminal(`\n[SYSTEM] Process ${pid} Terminated with code ${msg.code}\n`);
        activeWorkers.delete(pid);
        updateWorkerGrid();
        loadQueue();     // Refresh queue table to show updated status

        // Auto-pilot: if this is the current auto-pilot job, advance to the next one
        if (window.autoPilotRunning && pid === window.autoPilotCurrentPid) {
            appendTerminal(`\n[AUTO-PILOT] Job complete (exit ${msg.code}). Advancing queue in 3s...\n`);
            setTimeout(_launchNextInQueue, 3000);
        }
    }
};

ws.onclose = () => {
    appendTerminal(`\n[SYSTEM] WebSocket Disconnected. Backend may be down.\n`);
}

function updateWorkerState(pid, logLine) {
    // Only track "apply_" processes for the grid
    if (!pid.startsWith("apply_")) return;

    if (!activeWorkers.has(pid)) {
        activeWorkers.set(pid, {
            id: pid,
            title: pid.replace("apply_", "").replace(/_/g, " "),
            lastAction: "Initializing...",
            turns: 0
        });
    }

    const state = activeWorkers.get(pid);
    
    // Extract info from logs
    if (logLine.includes("Turn")) {
        const turnMatch = logLine.match(/Turn (\d+)/);
        if (turnMatch) state.turns = turnMatch[1];
        state.lastAction = logLine.trim();
    } else if (logLine.includes("Navigating")) {
        state.lastAction = "Navigating to portal...";
    } else if (logLine.includes("ACTION_REQUIRED:PENDING_APPROVAL")) {
        state.lastAction = "⚠️ WAITING FOR YOU";
    } else if (logLine.includes("click") || logLine.includes("type") || logLine.includes("upload")) {
        state.lastAction = logLine.trim();
    }

    updateWorkerGrid();
}

function updateWorkerGrid() {
    const grid = document.getElementById('active-workers-grid');
    if (activeWorkers.size === 0) {
        grid.innerHTML = '<div class="no-workers-msg">No active bots. Start an application from the Execution Hub.</div>';
        return;
    }

    grid.innerHTML = "";
    activeWorkers.forEach(w => {
        const card = document.createElement('div');
        card.className = "worker-card active bounce-in";
        card.innerHTML = `
            <div class="worker-header">
                <span class="worker-id">${w.id}</span>
                <button class="btn-clear" onclick="stopProcess('${w.id}')">✕</button>
            </div>
            <h4>${w.title}</h4>
            <div class="worker-status">
                <span class="status-dot"></span>
                <span>Active - Turn ${w.turns}</span>
            </div>
            <div class="last-action">${w.lastAction}</div>
        `;
        grid.appendChild(card);
    });
}

function appendTerminal(text) {
    const term = document.getElementById('terminal-emulator');
    
    // Process ANSI Colors to HTML (Basic mapping)
    let processed = text.replace(/\[bold\]/g, '<b>').replace(/\[\/bold\]/g, '</b>')
                        .replace(/\[dim\]/g, '<span style="color:var(--text-muted)">').replace(/\[\/dim\]/g, '</span>')
                        .replace(/\[green\]/g, '<span style="color:var(--success)">').replace(/\[\/green\]/g, '</span>')
                        .replace(/\[red\]/g, '<span style="color:var(--danger)">').replace(/\[\/red\]/g, '</span>')
                        .replace(/\[yellow\]/g, '<span style="color:var(--warning)">').replace(/\[\/yellow\]/g, '</span>');

    term.innerHTML += processed;
    term.scrollTop = term.scrollHeight; // Auto-scroll
}

// Sending Approval Input
async function sendApproval(choice) {
    const pid = window.activeApprovalPid || "default";
    document.getElementById('approval-overlay').classList.add('hidden');
    appendTerminal(`\n[SYSTEM] Sent Approval Hook to ${pid}: ${choice}\n`);
    await fetch(`/api/process/input/${pid}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input_text: choice + "\n" })
    });
}

// Doctor Output
async function runDoctor() {
    appendTerminal(`\n[SYSTEM] Running Doctor Diagnostics...\n`);
    await fetch('/api/process/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'doctor', args: [] })
    });
}

function closeDoctor() {
    document.getElementById('doctor-overlay').classList.add('hidden');
}

// Initialize
loadDashboard();
