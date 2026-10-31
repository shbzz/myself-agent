"""Bounded planning requests and a host-owned final acceptance gate."""

from pydantic import Field, field_validator

from myself_agent.general_settings import ConfigurationError
from myself_agent.orchestration_models import (
    Contract,
    Flow,
    Identifier,
    Node,
    WorkflowInput,
    task_nodes,
    validate_team,
)


class PlanInput(Contract):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(min_length=1, max_length=16000)
    team_id: Identifier
    planner_id: Identifier
    verification_skills: list[str] = Field(min_length=1, max_length=24)
    acceptance_checks: list[str] = Field(min_length=1, max_length=8)
    max_steps: int = Field(default=48, ge=2, le=96)
    reasoning_effort: str = Field(default="off", pattern=r"^[a-zA-Z0-9_-]{1,24}$")

    @field_validator("verification_skills")
    @classmethod
    def skills(cls, values):
        if any(not value.strip() or len(value) > 80 for value in values):
            raise ValueError("Verification skills must be nonempty and bounded")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("acceptance_checks")
    @classmethod
    def commands(cls, values):
        if any(not value.strip() or len(value) > 8000 for value in values):
            raise ValueError("Acceptance commands must be nonempty and bounded")
        if len(values) != len(set(values)):
            raise ValueError("Acceptance commands must be distinct")
        return values


class PlanApproval(Contract):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def compile_plan(proposal, request, team, identifier):
    """Validate generated DSL, forbid invented checks, append frozen acceptance."""
    workflow = WorkflowInput.model_validate(proposal)
    agents = [a["definition"] for a in team["agents"]]
    if workflow.max_steps > request["max_steps"]:
        raise ConfigurationError("Planner exceeded the approved step budget")
    if any(node.id == "acceptance_gate" for node in workflow.flow.nodes):
        raise ConfigurationError("acceptance_gate is reserved for the platform")
    tasks = list(task_nodes(workflow.flow))
    if not any(node.kind == "agent" for node in tasks):
        raise ConfigurationError("A plan must contain an execution task")
    for node in tasks:
        if node.kind == "verify" and not set(node.checks) <= set(request["acceptance_checks"]):
            raise ConfigurationError("Planner cannot introduce or replace acceptance commands")
    gate = Node(
        id="acceptance_gate",
        kind="verify",
        depends_on=[node.id for node in workflow.flow.nodes],
        prompt="Independently verify all produced outputs against the frozen user acceptance.",
        required_skills=request["verification_skills"],
        checks=request["acceptance_checks"],
    )
    compiled = WorkflowInput(
        id="plan-" + identifier,
        name=workflow.name,
        flow=Flow(nodes=[*workflow.flow.nodes, gate]),
        max_steps=request["max_steps"],
    )
    validate_team(compiled.flow, agents, team["configuration"])
    return compiled.model_dump()
