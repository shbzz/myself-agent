"""Authenticated workspace-scoped knowledge APIs; no remote fetch or model writes."""

from typing import Literal

from fastapi import APIRouter, Query, Request

from myself_agent.knowledge_models import (
    MemoryDecision,
    MemoryProposal,
    RetrievalInput,
    SourceInput,
)
from myself_agent.knowledge_search import retrieve

router = APIRouter(prefix="/next/api/knowledge")
KINDS = {"sources": "source", "memories": "memory"}


@router.post("/sources", status_code=201)
def save_source(body: SourceInput, request: Request):
    return request.app.state.manager.store.knowledge.save_source(body)


@router.post("/memories", status_code=201)
def propose_memory(body: MemoryProposal, request: Request):
    return request.app.state.manager.store.knowledge.propose(body)


@router.post("/memories/{identifier}/review")
def review_memory(identifier: str, body: MemoryDecision, request: Request):
    return request.app.state.manager.store.knowledge.decide(identifier, body)


@router.post("/retrieve")
def preview(body: RetrievalInput, request: Request):
    store = request.app.state.manager.store
    agent = store.config.get("agent", body.agent_id)
    snapshot = store.knowledge.freeze(body.session_id, [agent])
    return retrieve(snapshot, body.agent_id, body.query)


@router.get("/{kind}")
def records(
    kind: Literal["sources", "memories"],
    request: Request,
    workspace_id: str = Query(pattern=r"^[a-f0-9]{32}$"),
):
    return {kind: request.app.state.manager.store.knowledge.listing(KINDS[kind], workspace_id)}


@router.get("/{kind}/{identifier}/versions")
def versions(
    kind: Literal["sources", "memories"],
    identifier: str,
    request: Request,
    workspace_id: str = Query(pattern=r"^[a-f0-9]{32}$"),
):
    return {
        "versions": request.app.state.manager.store.knowledge.versions(
            KINDS[kind], workspace_id, identifier
        )
    }


@router.get("/{kind}/{identifier}")
def record(
    kind: Literal["sources", "memories"],
    identifier: str,
    request: Request,
    workspace_id: str = Query(pattern=r"^[a-f0-9]{32}$"),
    version: int | None = Query(default=None, ge=1),
):
    return request.app.state.manager.store.knowledge.get(
        KINDS[kind], workspace_id, identifier, version
    )
