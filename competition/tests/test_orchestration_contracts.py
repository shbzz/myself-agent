"""Contract tests for the declarative orchestration data models."""

import pytest
from pydantic import ValidationError

from myself_agent.general_settings import ConfigurationError
from myself_agent.orchestration_models import (
    AgentInput,
    Flow,
    Node,
    TeamInput,
    WorkflowInput,
    validate_team,
)

CONFIGURATION = {
    "policy": {"web_enabled": False, "browser_enabled": False},
    "mcp_servers": [],
}


def agent_node(node_id: str, **overrides) -> Node:
    values = {"id": node_id, "prompt": "do the work", "required_skills": ["work"]}
    values.update(overrides)
    return Node(**values)


def verifier_node(node_id: str, **overrides) -> Node:
    values = {
        "id": node_id,
        "kind": "verify",
        "prompt": "check the work",
        "required_skills": ["review"],
        "checks": ["pytest -q"],
    }
    values.update(overrides)
    return Node(**values)


def subworkflow_node(node_id: str, body: Flow, **overrides) -> Node:
    values = {"id": node_id, "kind": "subworkflow", "body": body}
    values.update(overrides)
    return Node(**values)


def loop_node(node_id: str, body: Flow, until: str, **overrides) -> Node:
    values = {"id": node_id, "kind": "loop", "body": body, "until": until}
    values.update(overrides)
    return Node(**values)


def condition_node(node_id: str, source: str, **overrides) -> Node:
    values = {
        "id": node_id,
        "kind": "condition",
        "source": source,
        "depends_on": [source],
        "on_true": Flow(nodes=[agent_node(f"{node_id}yes")]),
        "on_false": Flow(nodes=[agent_node(f"{node_id}no")]),
    }
    values.update(overrides)
    return Node(**values)


def team_agent(
    agent_id: str,
    skills: list[str],
    tools: list[str] | None = None,
    role: str = "worker",
) -> dict:
    data = AgentInput(name=agent_id, role=role, skills=skills, tools=tools or []).model_dump()
    data["id"] = agent_id
    return data


def test_agent_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        AgentInput(name="worker", skills=["work"], tools=["unknown_tool"])


def test_verifier_rejects_write_access():
    with pytest.raises(ValidationError):
        verifier_node("review", access="write")


def test_flow_rejects_cycle():
    nodes = [
        agent_node("alpha", depends_on=["beta"]),
        agent_node("beta", depends_on=["alpha"]),
    ]
    with pytest.raises(ValidationError):
        Flow(nodes=nodes)


def test_loop_accepts_bounded_build_review_cycle():
    body = Flow(
        nodes=[
            agent_node("build"),
            verifier_node("review", depends_on=["build"], repair_targets=["build"]),
        ]
    )
    flow = Flow(nodes=[loop_node("repairloop", body=body, until="review", max_iterations=4)])
    workflow = WorkflowInput(name="repair", flow=flow)
    assert workflow.flow.nodes[0].max_iterations == 4


def test_team_rejects_single_member():
    with pytest.raises(ValidationError):
        TeamInput(name="team", agent_ids=["alpha"])


def test_team_rejects_duplicate_members():
    with pytest.raises(ValidationError):
        TeamInput(name="team", agent_ids=["alpha", "alpha"])


def test_team_rejects_path_like_agent_id():
    with pytest.raises(ValidationError):
        TeamInput(name="team", agent_ids=["alpha", "../bad"])


def test_verifier_rejects_mutating_tool():
    with pytest.raises(ValidationError):
        AgentInput(name="reviewer", role="verifier", skills=["review"], tools=["mcp_call"])


def test_read_node_rejects_write_tool():
    with pytest.raises(ValidationError):
        agent_node("reader", access="read", required_tools=["write_file"])


def test_subworkflow_rejects_prompt():
    body = Flow(nodes=[agent_node("inner")])
    with pytest.raises(ValidationError):
        subworkflow_node("subone", body, prompt="ignored")


def test_agent_rejects_max_iterations():
    with pytest.raises(ValidationError):
        agent_node("worker", max_iterations=2)


def test_node_rejects_blank_skill():
    with pytest.raises(ValidationError):
        agent_node("worker", required_skills=["   "])


def test_flow_rejects_unknown_dependency():
    with pytest.raises(ValidationError):
        Flow(nodes=[agent_node("worker", depends_on=["ghost"])])


def test_verify_repair_target_must_be_ancestor():
    nodes = [
        agent_node("build"),
        agent_node("side"),
        verifier_node("review", depends_on=["build"], repair_targets=["side"]),
    ]
    with pytest.raises(ValidationError):
        Flow(nodes=nodes)


def test_loop_until_must_reference_verifier():
    body = Flow(nodes=[agent_node("build")])
    with pytest.raises(ValidationError):
        loop_node("retryloop", body=body, until="build")


def test_loop_rejects_iteration_limit_above_four():
    body = Flow(
        nodes=[
            agent_node("build"),
            verifier_node("review", depends_on=["build"]),
        ]
    )
    with pytest.raises(ValidationError):
        loop_node("retryloop", body=body, until="review", max_iterations=5)


def test_condition_with_verify_source_builds_workflow():
    flow = Flow(
        nodes=[
            agent_node("build"),
            verifier_node("review", depends_on=["build"]),
            condition_node("decide", "review"),
            subworkflow_node("followup", Flow(nodes=[agent_node("finalize")])),
        ]
    )
    workflow = WorkflowInput(name="conditional", flow=flow)
    kinds = [node.kind for node in workflow.flow.nodes]
    assert kinds == ["agent", "verify", "condition", "subworkflow"]


def test_condition_rejects_non_verify_source():
    with pytest.raises(ValidationError):
        Flow(
            nodes=[
                agent_node("build"),
                verifier_node("review", depends_on=["build"]),
                condition_node("decide", "build"),
            ]
        )


def test_workflow_rejects_nesting_deeper_than_three():
    body = Flow(nodes=[agent_node("worker")])
    for name in ("subfour", "subthree", "subtwo", "subone"):
        body = Flow(nodes=[subworkflow_node(name, body)])
    with pytest.raises(ValidationError):
        WorkflowInput(name="nested", flow=body)


def test_node_skills_are_stripped_and_deduplicated():
    node = agent_node("worker", required_skills=[" work ", "work", " review "])
    assert node.required_skills == ["work", "review"]


def test_agent_input_skills_are_stripped_and_deduplicated():
    agent = AgentInput(name="worker", skills=[" work ", "work", "review"])
    assert agent.skills == ["work", "review"]


def test_validate_team_rejects_skill_mismatch():
    flow = Flow(nodes=[agent_node("task", required_skills=["write"])])
    agents = [team_agent("worker", skills=["plan"])]
    with pytest.raises(ConfigurationError):
        validate_team(flow, agents, CONFIGURATION)


@pytest.mark.parametrize("tool", ["web_read", "browser_read"])
def test_validate_team_rejects_disabled_web_tool(tool):
    flow = Flow(
        nodes=[
            agent_node(
                "task",
                required_skills=["research"],
                required_tools=[tool],
            )
        ]
    )
    agents = [team_agent("worker", skills=["research"], tools=[tool])]
    with pytest.raises(ConfigurationError):
        validate_team(flow, agents, CONFIGURATION)


def test_validate_team_accepts_matching_agent():
    flow = Flow(
        nodes=[
            agent_node(
                "task",
                required_skills=["work"],
                required_tools=["read_file"],
            )
        ]
    )
    agents = [team_agent("worker", skills=["work"], tools=["read_file"])]
    validate_team(flow, agents, CONFIGURATION)
