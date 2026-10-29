"""Supervise one bounded Agent process; persist cancellation and restart outcomes."""

import fcntl
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from myself_agent.general_store import AgentStore


class AgentManager:
    def __init__(self, root, config_path):
        self.store = AgentStore(root)
        self.config_path = config_path
        self.lock_file = (self.store.root / "general.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("Another Agent worker owns this data directory") from None
        self.lock = threading.Lock()
        self.processes = {}
        with self.store.connect() as db:
            active = [
                r["id"]
                for r in db.execute("SELECT id FROM runs WHERE status IN ('queued','running')")
            ]
        for run_id in active:
            self.cleanup(run_id)
            self.store.status(run_id, "interrupted", "ServerRestart")
            self.store.event(run_id, "run.interrupted", {"reason": "ServerRestart"})
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, session_id, request_id, text):
        with self.lock:
            run, created = self.store.submit(session_id, request_id, text)
            if created:
                self.store.event(run["id"], "run.queued", {})
                self.pool.submit(self.execute, run["id"])
        return run

    def execute(self, run_id):
        with self.lock:
            if self.store.run(run_id)["status"] != "queued":
                return
            self.store.status(run_id, "running")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "myself_agent.general_runtime",
                    "--root",
                    str(self.store.root),
                    "--run-id",
                    run_id,
                    "--config",
                    str(self.config_path),
                ],
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).parent.parent),
                    "LANGSMITH_TRACING": "false",
                    "LANGCHAIN_TRACING_V2": "false",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes[run_id] = process
        try:
            code = process.wait(timeout=600)
            with self.lock:
                if self.store.run(run_id)["status"] == "running":
                    status = "succeeded" if code == 0 else "failed"
                    self.store.status(run_id, status, None if code == 0 else "AgentExecutionError")
                    self.store.event(run_id, "run.status", {"status": status})
        except subprocess.TimeoutExpired:
            self.cancel(run_id, "Timeout")
        finally:
            self.cleanup(run_id)
            with self.lock:
                self.processes.pop(run_id, None)

    @staticmethod
    def cleanup(run_id):
        subprocess.run(
            ["sudo", "-n", "docker", "rm", "-f", "myself-agent-general-" + run_id],
            capture_output=True,
            timeout=15,
            check=False,
        )

    def cancel(self, run_id, reason="UserStopped"):
        with self.lock:
            if self.store.run(run_id)["status"] not in ("running", "queued"):
                return self.store.run(run_id)
            self.store.status(run_id, "cancelled", reason)
            process = self.processes.get(run_id)
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            self.cleanup(run_id)
            self.store.event(run_id, "run.cancelled", {"reason": reason})
            return self.store.run(run_id)

    def close(self):
        with self.store.connect() as db:
            active = [
                r["id"]
                for r in db.execute("SELECT id FROM runs WHERE status IN ('running','queued')")
            ]
        for run_id in active:
            self.cancel(run_id, "ServerStopping")
        self.pool.shutdown(wait=True)
        self.lock_file.close()
