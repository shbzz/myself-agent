"""Planning drafts are inert until an exact digest is explicitly approved."""

import hashlib
import json
import os
from pathlib import Path

from myself_agent.general_settings import ConfigurationError
from myself_agent.jobs import BusyError
from myself_agent.planning_models import compile_plan


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def workspace_evidence(store, session_id):
    from myself_agent.orchestration_runtime import artifact_manifest

    directory = store.directory(store.session(session_id)["workspace_id"])
    manifest = artifact_manifest(directory)
    paths, truncated = [], False
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        for name in sorted(files):
            if len(paths) == 100:
                truncated = True
                break
            paths.append(str((Path(parent) / name).relative_to(directory)))
        if truncated:
            break
    return {
        "manifest": manifest,
        "file_paths": paths,
        "inventory_limit": 100,
        "inventory_truncated": truncated,
    }


class PlanningStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS planning_jobs (
                run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
                proposal TEXT, digest TEXT, usage TEXT, executed_run_id TEXT)
            """)

    def snapshot(self, run_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM planning_jobs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def prepare(self, request):
        # Idempotency freezes the original request/configuration, not today's edits.
        with self.store.connect() as db:
            prior = db.execute(
                "SELECT p.snapshot FROM planning_jobs p JOIN runs r ON r.id=p.run_id "
                "WHERE r.request_id=?",
                (request["request_id"],),
            ).fetchone()
        if prior:
            snapshot = json.loads(prior[0])
            if snapshot["request"] != request:
                raise BusyError("同一编号不能用于不同规划请求")
            return snapshot
        team = self.store.orchestration.prepare_team(
            request["session_id"], request["team_id"], request["reasoning_effort"]
        )
        planner = next(
            (a for a in team["agents"] if a["definition"]["id"] == request["planner_id"]), None
        )
        if (
            not planner
            or not planner["definition"]["enabled"]
            or planner["definition"]["role"] != "planner"
        ):
            raise ConfigurationError("An enabled team planner is required")
        if not any(
            a["definition"]["enabled"]
            and a["definition"]["role"] == "verifier"
            and set(request["verification_skills"]) <= set(a["definition"]["skills"])
            for a in team["agents"]
        ):
            raise ConfigurationError("No verifier has the required acceptance skills")
        return {
            "configuration": planner["configuration"],
            "team_snapshot": team,
            "request": request,
            "workspace": workspace_evidence(self.store, request["session_id"]),
        }

    def finish(self, run_id, proposal, usage):
        snapshot = self.snapshot(run_id)
        workflow = compile_plan(proposal, snapshot["request"], snapshot["team_snapshot"], run_id)
        digest = hashlib.sha256(
            canonical({"snapshot": snapshot, "workflow": workflow}).encode()
        ).hexdigest()
        with self.store.connect() as db:
            updated = db.execute(
                "UPDATE planning_jobs SET proposal=?,digest=?,usage=? WHERE run_id=? "
                "AND proposal IS NULL AND EXISTS "
                "(SELECT 1 FROM runs WHERE id=? AND status='running')",
                (canonical(workflow), digest, canonical(usage), run_id, run_id),
            )
        if not updated.rowcount:
            raise BusyError("规划已停止或已生成，拒绝迟到结果")
        return digest

    def view(self, run_id):
        with self.store.connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT p.*,r.status FROM planning_jobs p JOIN runs r ON r.id=p.run_id "
                "WHERE p.run_id=?",
                (run_id,),
            ).fetchone()
        if not row:
            raise KeyError(run_id)
        snapshot = json.loads(row["snapshot"])
        return {
            "id": run_id,
            "status": row["status"],
            "request": snapshot["request"],
            "workspace": snapshot["workspace"],
            "team": snapshot["team_snapshot"]["team"],
            "policy": snapshot["team_snapshot"]["configuration"]["policy"],
            "revisions": snapshot["team_snapshot"]["configuration"]["revisions"],
            "models": [
                {
                    "agent_id": member["definition"]["id"],
                    "profile": member["configuration"]["profile"],
                    "provider_id": member["configuration"]["provider"]["id"],
                    "provider_version": member["configuration"]["provider"]["version"],
                }
                for member in snapshot["team_snapshot"]["agents"]
                if member["configuration"]
            ],
            "agents": [a["definition"] for a in snapshot["team_snapshot"]["agents"]],
            "workflow": json.loads(row["proposal"]) if row["proposal"] else None,
            "digest": row["digest"],
            "usage": json.loads(row["usage"]) if row["usage"] else None,
            "executed_run_id": row["executed_run_id"],
            "approval_required": row["status"] == "succeeded" and row["executed_run_id"] is None,
        }

    def approval(self, run_id, digest):
        draft = self.view(run_id)
        if draft["status"] != "succeeded" or draft["digest"] != digest:
            raise BusyError("只能批准已完成且摘要完全匹配的计划")
        snapshot = self.snapshot(run_id)
        request = snapshot["request"]
        if draft["executed_run_id"]:
            return snapshot, draft
        current = self.store.orchestration.prepare_team(
            request["session_id"], request["team_id"], request["reasoning_effort"]
        )
        if canonical(current) != canonical(snapshot["team_snapshot"]):
            raise BusyError("团队、模型或策略已变更，请重新规划后确认")
        if (
            workspace_evidence(self.store, request["session_id"])["manifest"]
            != snapshot["workspace"]["manifest"]
        ):
            raise BusyError("工作区已变更，请重新规划后确认")
        return snapshot, draft
