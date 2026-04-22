"""Async subprocess manager for WebSocket streaming."""

import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

class ProcessManager:
    def __init__(self):
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self._output_queue: asyncio.Queue = asyncio.Queue()

    def is_running(self, process_id: Optional[str] = None) -> bool:
        if process_id:
            p = self.processes.get(process_id)
            return p is not None and p.returncode is None
        return any(p.returncode is None for p in self.processes.values())

    async def start(self, cmd: list[str], process_id: str = "default"):
        if self.is_running(process_id):
            return
            
        logger.info(f"Starting process [{process_id}]: {' '.join(cmd)}")
        
        import os
        spawn_env = os.environ.copy()
        spawn_env.update({"PYTHONUNBUFFERED": "1", "FORCE_COLOR": "1"})
        
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=spawn_env
        )
        self.processes[process_id] = p
        
        # Start background reader
        asyncio.create_task(self._read_stdout(p, process_id))

    async def _read_stdout(self, process, process_id):
        """Asynchronously read lines from stdout and push to queue."""
        if not process.stdout:
            return
            
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                    
                decoded = line.decode('utf-8', errors='replace')

                # Strip ANSI escape codes
                import re
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                clean_decoded = ansi_escape.sub('', decoded)
                
                # Check for approval hook
                if "ACTION_REQUIRED:PENDING_APPROVAL" in clean_decoded:
                    import re
                    match = re.search(r"ACTION_REQUIRED:PENDING_APPROVAL:(\d+):(\d+):(\w+)", clean_decoded)
                    if match:
                        await self._output_queue.put({
                            "type": "APPROVAL_REQUEST", 
                            "process_id": process_id,
                            "worker_id": match.group(1),
                            "port": match.group(2),
                            "reason": match.group(3)
                        })
                    else:
                        await self._output_queue.put({"type": "APPROVAL_REQUEST", "process_id": process_id})
                elif "Type 'y' to SUBMIT" in clean_decoded:
                    await self._output_queue.put({"type": "APPROVAL_REQUEST", "process_id": process_id})
                
                await self._output_queue.put({
                    "type": "log",
                    "process_id": process_id,
                    "data": clean_decoded
                })
        except Exception as e:
            logger.error(f"Error reading stdout for {process_id}: {e}")
        finally:
            await process.wait()
            await self._output_queue.put({"type": "exit", "process_id": process_id, "code": process.returncode})
            if process_id in self.processes:
                del self.processes[process_id]

    async def write_stdin(self, text: str, process_id: str = "default"):
        p = self.processes.get(process_id)
        if p and p.returncode is None and p.stdin:
            p.stdin.write(text.encode('utf-8'))
            await p.stdin.drain()

    async def stop(self, process_id: Optional[str] = None):
        if process_id:
            p = self.processes.get(process_id)
            if p and p.returncode is None:
                try:
                    import platform
                    import subprocess
                    if platform.system() == "Windows":
                        # Force kill the entire process tree on Windows
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], 
                                     capture_output=True, check=False)
                    else:
                        p.terminate()
                        try:
                            await asyncio.wait_for(p.wait(), timeout=3.0)
                        except asyncio.TimeoutError:
                            p.kill()
                except Exception as e:
                    logger.error(f"Error stopping process {process_id}: {e}")
        else:
            # Stop everything tracked
            for pid in list(self.processes.keys()):
                await self.stop(pid)
            
            # EMERGENCY: Also kill any stray applypilot processes on the system
            # This handles orphans after a server restart.
            try:
                import platform
                import subprocess
                if platform.system() == "Windows":
                    # Kill any python process running applypilot CLI that we've lost track of
                    # We use PowerShell to filter command lines precisely
                    cmd = 'Get-CimInstance Win32_Process -Filter "name = \'python.exe\'" | Where-Object { $_.CommandLine -like "*applypilot.cli*" } | Stop-Process -Force'
                    subprocess.run(["powershell", "-Command", cmd], capture_output=True, check=False)
                    # Also clean up any stray chrome instances
                    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], capture_output=True, check=False)
            except Exception as e:
                logger.error(f"Error in global system cleanup: {e}")

    async def listen_and_broadcast(self, broadcast_fn: Callable):
        while True:
            msg = await self._output_queue.get()
            import json
            await broadcast_fn(json.dumps(msg))
            self._output_queue.task_done()
