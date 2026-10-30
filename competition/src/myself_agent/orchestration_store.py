"""Versioned team/workflow registry and durable task evidence beside general sessions."""

import json
import uuid

from myself_agent.general_settings import ConfigurationError, ConfigurationStore
from myself_agent.general_store import now
from myself_agent.jobs import BusyError
from myself_agent.orchestration_models import (
    AgentInput,
    TeamInput,
    WorkflowInput,
    task_nodes,
    validate_team,
)


class OrchestrationStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orchestrations (
                  run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS orchestration_tasks (
                  run_id TEXT NOT NULL, path TEXT NOT NULL, node_id TEXT NOT NULL,
                  kind TEXT NOT NULL, status TEXT NOT NULL, agent_id TEXT,
                  attempt_id TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
                  result TEXT, PRIMARY KEY(run_id,path));
            """)

    def save(self, kind, body):
        expected = {"agent": AgentInput, "team": TeamInput, "workflow": WorkflowInput}
        if kind not in expected or not isinstance(body, expected[kind]):
            raise ConfigurationError("Invalid orchestration definition")
        if kind == "agent" and body.model_profile_id:
            self.store.config.get("model", body.model_profile_id)
        if kind == "team":
            for identifier in body.agent_ids:
                self.store.config.get("agent", identifier)
        identifier = body.id or uuid.uuid4().hex
        return self.store.config.write(kind, identifier, body.model_dump() | {"id": identifier})

    def prepare(self, session_id, team_id, workflow_id, effort):
        config = self.store.config.resolve(session_id=session_id)
        team = self.store.config.get("team", team_id)
        workflow = self.store.config.get("workflow", workflow_id)
        flow = WorkflowInput.model_validate(
            {k: v for k, v in workflow.items() if k != "version"}
        ).flow
        agents = []
        for identifier in team["agent_ids"]:
            definition = self.store.config.get("agent", identifier)
            if not definition["enabled"]:
                agents.append({"definition": definition, "configuration": None})
                continue
            model_config = self.store.config.resolve(
                session_id=session_id,
                override=config["policy"]
                | {"model_profile_id": definition["model_profile_id"] or config["profile"]["id"]},
            )
            if effort not in model_config["profile"]["reasoning_levels"]:
                raise ConfigurationError("A team model does not support this reasoning effort")
            if not model_config["profile"]["tools"]:
                raise ConfigurationError("Team models must declare tool support")
            agents.append({"definition": definition, "configuration": model_config})
        tasks = list(task_nodes(flow))
        if not any(n.kind == "agent" for n in tasks) or not any(n.kind == "verify" for n in tasks):
            raise ConfigurationError("A run requires both execution and independent verification")
        validate_team(flow, [a["definition"] for a in agents], config)
        return {"configuration": config, "team": team, "workflow": workflow, "agents": agents}

    def submission(self, session_id, request_id, text, team_id, workflow_id, effort):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT r.*,o.snapshot FROM runs r LEFT JOIN orchestrations o ON r.id=o.run_id "
                "WHERE r.request_id=?",
                (request_id,),
            ).fetchone()
        if row:
            previous = json.loads(row["snapshot"]) if row["snapshot"] else None
            if (
                not previous
                or row["session_id"] != session_id
                or row["text"] != text
                or (
                    row["reasoning_effort"] != effort
                    or previous["team"]["id"] != team_id
                    or previous["workflow"]["id"] != workflow_id
                )
            ):
                raise BusyError("同一请求编号不能用于不同编排内容")
            return previous
        return self.prepare(session_id, team_id, workflow_id, effort)

    def snapshot(self, run_id, *, public=False):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM orchestrations WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(run_id)
        value = json.loads(row["snapshot"])
        if public:
            value["configuration"] = ConfigurationStore.public_snapshot(value["configuration"])
            value["agents"] = [
                dict(
                    a,
                    configuration=(
                        ConfigurationStore.public_snapshot(a["configuration"])
                        if a["configuration"] is not None
                        else None
                    ),
                )
                for a in value["agents"]
            ]
        return value

    def task(self, run_id, path, node, status, *, agent_id=None, attempt_id=None, result=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not parent:
                raise KeyError(run_id)
            if parent[0] not in ("queued", "running"):
                existing = db.execute(
                    "SELECT status FROM orchestration_tasks WHERE run_id=? AND path=?",
                    (run_id, path),
                ).fetchone()
                if existing and existing[0] != "running":
                    return
                status = "cancelled" if parent[0] == "cancelled" else "interrupted"
                result = {"reason": parent[0]}
            db.execute(
                "INSERT INTO orchestration_tasks VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,path) DO UPDATE SET status=excluded.status, "
                "updated=excluded.updated, result=excluded.result",
                (
                    run_id,
                    path,
                    node.id,
                    node.kind,
                    status,
                    agent_id,
                    attempt_id,
                    now(),
                    now(),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                ),
            )
        self.store.event(
            run_id,
            "orchestration.task",
            {
                "path": path,
                "node_id": node.id,
                "kind": node.kind,
                "status": status,
                "agent_id": agent_id,
                "attempt_id": attempt_id,
                "passed": result.get("passed") if result else None,
            },
        )

    def tasks(self, run_id):
        self.snapshot(run_id)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM orchestration_tasks WHERE run_id=? ORDER BY created,path", (run_id,)
            ).fetchall()
        return [
            dict(row) | {"result": json.loads(row["result"]) if row["result"] else None}
            for row in rows
        ]

    def finish(self, run_id, result):
        with self.store.connect() as db:
            updated = db.execute(
                "UPDATE orchestrations SET result=? WHERE run_id=? AND EXISTS "
                "(SELECT 1 FROM runs WHERE id=? AND status IN ('queued','running'))",
                (json.dumps(result, ensure_ascii=False), run_id, run_id),
            )
            return updated.rowcount == 1

    def interrupt(self, run_id, status, reason):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if parent and parent[0] == "cancelled":
                status = "cancelled"
            db.execute(
                "UPDATE orchestration_tasks SET status=?,updated=? "
                "WHERE run_id=? AND status='running'",
                (status, now(), run_id),
            )
            previous = db.execute(
                "SELECT result FROM orchestrations WHERE run_id=?", (run_id,)
            ).fetchone()
            if previous and previous[0] and json.loads(previous[0]).get("status") == "cancelled":
                return
            db.execute(
                "UPDATE orchestrations SET result=? WHERE run_id=?",
                (json.dumps({"status": status, "reason": reason}), run_id),
            )

    def run(self, run_id):
        snapshot = self.snapshot(run_id, public=True)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT result FROM orchestrations WHERE run_id=?", (run_id,)
            ).fetchone()
        return {
            "run": self.store.run(run_id),
            "snapshot": snapshot,
            "tasks": self.tasks(run_id),
            "result": json.loads(row["result"]) if row["result"] else None,
        }
