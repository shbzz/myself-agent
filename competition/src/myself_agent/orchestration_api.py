"""Backend-only team/workflow APIs, using the existing same-origin/auth boundary."""

import json
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field

from myself_agent.orchestration_models import (
    AgentInput,
    Contract,
    Identifier,
    TeamInput,
    WorkflowInput,
)

router = APIRouter(prefix="/next/api/orchestration")
KINDS = {"agents": "agent", "teams": "team", "workflows": "workflow"}


class WorkflowRunInput(Contract):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(min_length=1, max_length=16000)
    team_id: Identifier
    workflow_id: Identifier
    reasoning_effort: str = Field(default="off", pattern=r"^[a-zA-Z0-9_-]{1,24}$")


@router.post("/agents", status_code=201)
def save_agent(body: AgentInput, request: Request):
    return request.app.state.manager.store.orchestration.save("agent", body)


@router.post("/teams", status_code=201)
def save_team(body: TeamInput, request: Request):
    return request.app.state.manager.store.orchestration.save("team", body)


@router.post("/workflows", status_code=201)
def save_workflow(body: WorkflowInput, request: Request):
    return request.app.state.manager.store.orchestration.save("workflow", body)


@router.post("/runs", status_code=202)
def submit(body: WorkflowRunInput, request: Request):
    return request.app.state.manager.submit_workflow(**body.model_dump())


@router.get("/runs")
def runs(request: Request, session_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
    return {
        "runs": [
            run
            for run in request.app.state.manager.store.runs(session_id)
            if run["action"] == "orchestrate"
        ]
    }


@router.get("/runs/{run_id}")
def run(run_id: str, request: Request):
    return request.app.state.manager.store.orchestration.run(run_id)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str, request: Request):
    manager = request.app.state.manager
    manager.store.orchestration.snapshot(run_id)
    return manager.cancel(run_id)


@router.get("/runs/{run_id}/events")
def events(run_id: str, request: Request, after: int = Query(0, ge=0)):
    store = request.app.state.manager.store
    store.orchestration.snapshot(run_id)
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 200",
            (run_id, after),
        ).fetchall()
    return {"events": [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]}


@router.get("/{kind}")
def definitions(
    kind: Literal["agents", "teams", "workflows"],
    request: Request,
    skill: str | None = Query(None, min_length=1, max_length=80),
    enabled: bool | None = None,
):
    values = request.app.state.manager.store.config.list(KINDS[kind])
    if kind == "agents":
        if skill is not None:
            values = [v for v in values if skill.strip() in v["skills"]]
        if enabled is not None:
            values = [v for v in values if v["enabled"] == enabled]
    return {kind: values}


@router.get("/{kind}/{identifier}/versions")
def versions(kind: Literal["agents", "teams", "workflows"], identifier: str, request: Request):
    store = request.app.state.manager.store
    store.config.get(KINDS[kind], identifier)
    with store.connect() as db:
        rows = db.execute(
            "SELECT value,version FROM configuration WHERE kind=? AND id=? ORDER BY version DESC",
            (KINDS[kind], identifier),
        ).fetchall()
    return {"versions": [json.loads(row["value"]) | {"version": row["version"]} for row in rows]}
