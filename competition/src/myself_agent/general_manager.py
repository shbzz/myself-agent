"""Supervise one bounded Agent process; persist cancellation and restart outcomes."""

import fcntl
import os
import re
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
            self.store.orchestration.stop(run_id, "interrupted", "ServerRestart")
            self.store.event(run_id, "run.interrupted", {"reason": "ServerRestart"})
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(
        self,
        session_id,
        request_id,
        text,
        attachment_ids=(),
        reasoning_effort="low",
        model_profile_id=None,
        action="message",
    ):
        with self.lock:
            run, created = self.store.submit(
                session_id,
                request_id,
                text,
                attachment_ids,
                reasoning_effort,
                model_profile_id,
                action,
            )
            if created:
                self.store.event(run["id"], "run.queued", {})
                self.pool.submit(self.execute, run["id"])
        return run

    def submit_workflow(
        self, session_id, request_id, text, team_id, workflow_id, reasoning_effort="off"
    ):
        with self.lock:
            snapshot = self.store.orchestration.submission(
                session_id, request_id, text, team_id, workflow_id, reasoning_effort
            )
            run, created = self.store.submit(
                session_id,
                request_id,
                text,
                reasoning_effort=reasoning_effort,
                action="orchestrate",
                orchestration=snapshot,
            )
            if created:
                self.store.event(run["id"], "run.queued", {"action": "orchestrate"})
                self.pool.submit(self.execute, run["id"])
        return run

    def submit_plan(self, body):
        with self.lock:
            snapshot = self.store.planning.prepare(body)
            run, created = self.store.submit(
                body["session_id"],
                body["request_id"],
                body["text"],
                reasoning_effort=body["reasoning_effort"],
                action="plan",
                planning=snapshot,
            )
            if created:
                self.store.event(run["id"], "run.queued", {"action": "plan"})
                self.pool.submit(self.execute, run["id"])
            return run

    def approve_plan(self, plan_id, request_id, digest):
        from myself_agent.jobs import BusyError
        from myself_agent.planning_store import canonical

        with self.lock:
            frozen, draft = self.store.planning.approval(plan_id, digest)
            if draft["executed_run_id"]:
                previous = self.store.run(draft["executed_run_id"])
                if previous["request_id"] != request_id:
                    raise BusyError("计划已执行，请使用原请求编号查询")
                return previous
            body = frozen["request"]
            snapshot = frozen["team_snapshot"] | {
                "workflow": draft["workflow"],
                "approved_plan": {
                    "id": plan_id,
                    "digest": digest,
                    "workspace_manifest": frozen["workspace"]["manifest"],
                },
            }
            run, created = self.store.submit(
                body["session_id"],
                request_id,
                body["text"],
                reasoning_effort=body["reasoning_effort"],
                action="orchestrate",
                orchestration=snapshot,
                plan_approval={
                    "id": plan_id,
                    "digest": digest,
                    "proposal": canonical(draft["workflow"]),
                },
            )
            if created:
                self.store.event(
                    run["id"], "run.queued", {"action": "orchestrate", "approved_plan": plan_id}
                )
                self.pool.submit(self.execute, run["id"])
            return run

    def execute(self, run_id):
        with self.lock:
            if self.store.run(run_id)["status"] != "queued":
                return
            self.store.status(run_id, "running")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        {
                            "orchestrate": "myself_agent.orchestration_runtime",
                            "plan": "myself_agent.planning_runtime",
                        }.get(self.store.run(run_id)["action"], "myself_agent.general_runtime"),
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
            except Exception as error:
                self.store.orchestration.stop(run_id, "failed", type(error).__name__)
                self.store.event(run_id, "run.error", {"error_type": type(error).__name__})
                return
            self.processes[run_id] = process
        try:
            code = process.wait(
                timeout=self.store.run_configuration(run_id)["policy"]["timeout_seconds"]
            )
            with self.lock:
                if self.store.run(run_id)["status"] == "running":
                    status = "succeeded" if code == 0 else "failed"
                    if code == 0:
                        self.store.status(run_id, status)
                    else:
                        self.store.orchestration.stop(run_id, status, "AgentExecutionError")
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

        listing = subprocess.run(
            [
                "sudo",
                "-n",
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=myself-agent.parent=" + run_id,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        for identifier in listing.stdout.splitlines():
            if re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                subprocess.run(
                    ["sudo", "-n", "docker", "rm", "-f", identifier],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )

    def cancel(self, run_id, reason="UserStopped"):
        with self.lock:
            if self.store.run(run_id)["status"] not in ("running", "queued"):
                return self.store.run(run_id)
            self.store.orchestration.stop(run_id, "cancelled", reason)
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
