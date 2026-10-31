import json
import sqlite3
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_general import ScriptModel, call
from test_orchestration_api import QueuedManager
from test_orchestration_engine import new_workflow, review, work

from myself_agent.general_settings import ConfigurationError, MCPServerInput, PolicyInput
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError
from myself_agent.orchestration_models import AgentInput, Flow, WorkflowInput
from myself_agent.orchestration_runtime import run_orchestration
from myself_agent.planning_models import PlanInput
from myself_agent.planning_runtime import run_planning


def planning_case(root):
    store, baseline, workspace, session = new_workflow(root, [work(), review()])
    store.status(baseline["id"], "succeeded")
    store.orchestration.save(
        "agent",
        AgentInput(
            id="worker-a",
            name="Planner",
            role="planner",
            skills=["plan", "build"],
            tools=["read_file", "write_file", "edit_file", "execute"],
        ),
    )
    manager = QueuedManager(root, root / "unused.env")
    body = PlanInput(
        session_id=session["id"],
        request_id=uuid.uuid4().hex,
        text="Implement a small utility",
        team_id="team",
        planner_id="worker-a",
        verification_skills=["test"],
        acceptance_checks=["python -V"],
    ).model_dump()
    proposal = WorkflowInput(
        name="Generated plan", flow=Flow(nodes=[work(access="write")])
    ).model_dump()
    return manager, body, proposal, workspace


def finish_plan(manager, body, proposal):
    run = manager.submit_plan(body)
    manager.store.status(run["id"], "running")
    digest = manager.store.planning.finish(run["id"], proposal, {"model_calls": 1})
    manager.store.status(run["id"], "succeeded")
    return run, digest


def test_planning_is_tool_free_and_waits_for_explicit_digest_approval(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    run = manager.submit_plan(body)
    assert manager.submit_plan(body)["id"] == run["id"]
    assert manager.submissions == [run["id"]]
    with pytest.raises(BusyError):
        manager.approve_plan(run["id"], uuid.uuid4().hex, "a" * 64)
    manager.store.status(run["id"], "running")
    model = ScriptModel(responses=[AIMessage(content=json.dumps(proposal))])

    @contextmanager
    def factory():
        yield model

    view = run_planning(manager.store, run["id"], model_factory=factory)
    assert model.names == []
    assert view["usage"]["model_calls"] == 1
    assert view["executed_run_id"] is None
    assert manager.submissions == [run["id"]]
    assert "credential_ref" not in str(model.seen)
    assert view["workflow"]["flow"]["nodes"][-1]["checks"] == body["acceptance_checks"]
    manager.store.status(run["id"], "succeeded")
    request_id = uuid.uuid4().hex
    execution = manager.approve_plan(run["id"], request_id, view["digest"])
    assert execution["action"] == "orchestrate"
    assert manager.approve_plan(run["id"], request_id, view["digest"])["id"] == execution["id"]
    assert manager.submissions == [run["id"], execution["id"]]
    with pytest.raises(BusyError):
        manager.approve_plan(run["id"], uuid.uuid4().hex, view["digest"])


@pytest.mark.parametrize("change", ["digest", "workspace", "policy", "team"])
def test_approval_rejects_stale_or_changed_inputs(tmp_path, change):
    manager, body, proposal, workspace = planning_case(tmp_path)
    run, digest = finish_plan(manager, body, proposal)
    if change == "digest":
        digest = "0" * 64
    elif change == "workspace":
        (manager.store.directory(workspace["id"]) / "changed.txt").write_text("changed")
    elif change == "policy":
        manager.store.config.save_policy("global", PolicyInput(max_model_calls=12))
    else:
        manager.store.orchestration.save(
            "agent",
            AgentInput(id="worker-b", name="Changed", skills=["build"], tools=["read_file"]),
        )
    with pytest.raises(BusyError):
        manager.approve_plan(run["id"], uuid.uuid4().hex, digest)
    assert manager.store.planning.view(run["id"])["executed_run_id"] is None


@pytest.mark.parametrize("mutation", ["invented_check", "budget", "capability", "reserved"])
def test_generated_plan_cannot_weaken_acceptance_or_expand_authority(tmp_path, mutation):
    manager, body, proposal, _ = planning_case(tmp_path)
    if mutation == "invented_check":
        proposal["flow"]["nodes"].append(review(checks=["echo always-pass"]).model_dump())
    elif mutation == "budget":
        proposal["max_steps"] = 96
    elif mutation == "capability":
        proposal["flow"]["nodes"][0]["required_skills"] = ["unavailable"]
    else:
        proposal["flow"]["nodes"][0]["id"] = "acceptance_gate"
    run = manager.submit_plan(body)
    manager.store.status(run["id"], "running")
    with pytest.raises(ConfigurationError):
        manager.store.planning.finish(run["id"], proposal, {})
    assert manager.store.planning.view(run["id"])["workflow"] is None


def test_plan_submission_and_approval_are_atomic(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    with manager.store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_plan BEFORE INSERT ON planning_jobs "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        manager.submit_plan(body)
    assert not manager.submissions
    assert all(
        run["request_id"] != body["request_id"] for run in manager.store.runs(body["session_id"])
    )
    with manager.store.connect() as db:
        db.execute("DROP TRIGGER fail_plan")
    run, digest = finish_plan(manager, body, proposal)
    with manager.store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_approve BEFORE UPDATE OF executed_run_id ON planning_jobs "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        manager.approve_plan(run["id"], uuid.uuid4().hex, digest)
    assert manager.store.planning.view(run["id"])["executed_run_id"] is None
    assert len(manager.submissions) == 1
    assert not any(
        run["status"] in ("queued", "running") for run in manager.store.runs(body["session_id"])
    )


def test_cancelled_plan_cannot_receive_late_draft(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    run = manager.submit_plan(body)
    manager.cancel(run["id"])
    with pytest.raises(BusyError):
        manager.store.planning.finish(run["id"], proposal, {})
    assert manager.store.planning.view(run["id"])["workflow"] is None


def test_plan_request_id_cannot_change_acceptance(tmp_path):
    manager, body, _, _ = planning_case(tmp_path)
    manager.submit_plan(body)
    with pytest.raises(BusyError):
        manager.submit_plan(body | {"acceptance_checks": ["echo weaker"]})


def test_planning_api_uses_existing_security_and_exposes_drafts(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    with TestClient(create_app(tmp_path, manager_factory=QueuedManager)) as client:
        url = "/next/api/orchestration/plans"
        assert (
            client.post(url, json=body, headers={"Origin": "https://untrusted.example"}).status_code
            == 403
        )
        assert client.post(url, json=body | {"acceptance_checks": []}).status_code == 422
        response = client.post(url, json=body)
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert client.get(url + "/" + run_id).json()["workflow"] is None
        assert len(client.get(url, params={"session_id": body["session_id"]}).json()["plans"]) == 1
        manager.store.status(run_id, "running")
        digest = manager.store.planning.finish(run_id, proposal, {})
        manager.store.status(run_id, "succeeded")
        approved = client.post(
            url + "/" + run_id + "/approve", json={"request_id": uuid.uuid4().hex, "digest": digest}
        )
        assert approved.status_code == 202
        details = client.get("/next/api/orchestration/runs/" + approved.json()["id"]).json()
        assert details["snapshot"]["approved_plan"]["digest"] == digest
        assert "credential_ref" not in json.dumps(details)


def test_workspace_change_after_approval_refuses_before_model(tmp_path):
    manager, body, proposal, workspace = planning_case(tmp_path)
    run, digest = finish_plan(manager, body, proposal)
    execution = manager.approve_plan(run["id"], uuid.uuid4().hex, digest)
    manager.store.status(execution["id"], "running")
    (manager.store.directory(workspace["id"]) / "later.txt").write_text("changed")

    def unexpected(*args):
        pytest.fail("Changed workspace must be refused before a model call")

    with pytest.raises(ConfigurationError, match="Workspace changed"):
        run_orchestration(manager.store, execution["id"], model_factory=unexpected)


@pytest.mark.parametrize("kind", ["invalid_json", "tool_call", "truncated", "oversized"])
def test_bad_planner_output_never_becomes_an_approvable_plan(tmp_path, kind):
    manager, body, proposal, _ = planning_case(tmp_path)
    run = manager.submit_plan(body)
    manager.store.status(run["id"], "running")
    response = AIMessage(content=json.dumps(proposal))
    if kind == "invalid_json":
        response = AIMessage(content="not-json")
    elif kind == "tool_call":
        response = call("execute", {"command": "echo forbidden"})
    elif kind == "truncated":
        response.response_metadata = {"finish_reason": "length"}
    else:
        response.content = "x" * 65537
    model = ScriptModel(responses=[response])

    @contextmanager
    def factory():
        yield model

    with pytest.raises((ConfigurationError, json.JSONDecodeError)):
        run_planning(manager.store, run["id"], model_factory=factory)
    assert manager.store.planning.view(run["id"])["workflow"] is None
    assert len(model.seen) == 1
    assert manager.submissions == [run["id"]]


@pytest.mark.docker
@pytest.mark.parametrize("contents", ["42", "0"])
def test_approved_generated_plan_uses_frozen_independent_acceptance(tmp_path, settings, contents):
    manager, body, proposal, workspace = planning_case(tmp_path)
    body["acceptance_checks"] = [
        'python -I -c "from pathlib import Path; '
        "assert Path('/workspace/result.txt').read_text()=='42'\""
    ]
    run = manager.submit_plan(body)
    manager.store.status(run["id"], "running")

    @contextmanager
    def planner():
        yield ScriptModel(responses=[AIMessage(content=json.dumps(proposal))])

    plan = run_planning(manager.store, run["id"], model_factory=planner)
    directory = manager.store.directory(workspace["id"])
    assert list(directory.iterdir()) == []
    assert manager.submissions == [run["id"]]
    manager.store.status(run["id"], "succeeded")
    execution = manager.approve_plan(run["id"], uuid.uuid4().hex, plan["digest"])
    manager.store.status(execution["id"], "running")

    @contextmanager
    def executor(request):
        responses = [AIMessage(content='{"passed":true,"summary":"fixture model claims pass"}')]
        if request.node.kind == "agent":
            responses = [
                call("write_file", {"file_path": "/workspace/result.txt", "content": contents}),
                AIMessage(content="fixture output"),
            ]
        yield ScriptModel(responses=responses)

    result = run_orchestration(
        manager.store, execution["id"], model_factory=executor, image=settings.image
    )
    assert result["passed"] is (contents == "42")
    gate = manager.store.orchestration.tasks(execution["id"])[-1]
    assert gate["node_id"] == "acceptance_gate"
    assert [check["command"] for check in gate["result"]["checks"]] == body["acceptance_checks"]
    assert gate["result"]["review_passed"] is True


def test_saved_plan_can_be_reviewed_after_manager_restart(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    run, digest = finish_plan(manager, body, proposal)
    restarted = QueuedManager(tmp_path, tmp_path / "unused.env")
    view = restarted.store.planning.view(run["id"])
    assert view["approval_required"] and view["digest"] == digest
    execution = restarted.approve_plan(run["id"], uuid.uuid4().hex, digest)
    assert (
        restarted.store.orchestration.snapshot(execution["id"])["approved_plan"]["id"] == run["id"]
    )


def test_planner_sees_enabled_mcp_catalogue_without_private_metadata(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    manager.store.config.save_mcp_server(
        MCPServerInput(
            id="configured-mcp",
            name="Approved service",
            url="http://127.0.0.1:9876/mcp",
            api_key="fixture-planning-secret",
            allowed_tools=["lookup"],
        )
    )
    manager.store.config.save_policy("global", PolicyInput(mcp_server_ids=["configured-mcp"]))
    run = manager.submit_plan(body)
    manager.store.status(run["id"], "running")
    model = ScriptModel(responses=[AIMessage(content=json.dumps(proposal))])

    @contextmanager
    def factory():
        yield model

    view = run_planning(manager.store, run["id"], model_factory=factory)
    prompt = str(model.seen)
    assert "configured-mcp" in prompt and "lookup" in prompt
    assert "fixture-planning-secret" not in prompt + json.dumps(view)
    assert "credential_ref" not in prompt + json.dumps(view)
    assert "127.0.0.1:9876" not in prompt + json.dumps(view)
    assert model.names == []


def test_approved_plan_cannot_take_a_second_global_run_lease(tmp_path):
    manager, body, proposal, _ = planning_case(tmp_path)
    run, digest = finish_plan(manager, body, proposal)
    other = manager.submit_workflow(
        body["session_id"], uuid.uuid4().hex, "other job", "team", "flow"
    )
    with pytest.raises(BusyError):
        manager.approve_plan(run["id"], uuid.uuid4().hex, digest)
    assert manager.store.planning.view(run["id"])["executed_run_id"] is None
    manager.cancel(other["id"])
    assert manager.approve_plan(run["id"], uuid.uuid4().hex, digest)["status"] == "queued"
