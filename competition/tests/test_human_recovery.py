"""Checkpoint fixtures are deterministic, not paid-model performance evidence."""

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from test_general import ScriptModel, call
from test_orchestration_api import QueuedManager
from test_orchestration_engine import new_workflow, review, work

from myself_agent.general_manager import AgentManager
from myself_agent.general_settings import ConfigurationError
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError
from myself_agent.orchestration_models import Flow, Node, WorkflowInput
from myself_agent.orchestration_runtime import artifact_manifest, run_orchestration


def human(identifier="human", source="review"):
    return Node(id=identifier, kind="human", source=source, depends_on=[source])


@pytest.fixture
def fixture_runner(monkeypatch):
    visits = []

    class Runner:
        def __init__(self, store, run_id, *args, **kwargs):
            self.snapshot = store.orchestration.snapshot(run_id)
            self.directory = store.directory(
                store.session(store.run(run_id)["session_id"])["workspace_id"]
            )
            self.budget = SimpleNamespace(calls=0)
            self.web = SimpleNamespace(requests=0)
            self.mcp = SimpleNamespace(calls=0)
            self.usage = {}

        def __call__(self, request):
            if self.budget.calls >= self.snapshot["configuration"]["policy"]["max_model_calls"]:
                raise ConfigurationError("model budget exhausted")
            visits.append(request.path)
            self.budget.calls += 1
            self.web.requests += 1
            self.mcp.calls += 1
            self.usage[request.path] = {"fixture": {"total_tokens": 10}}
            if request.node.access == "write":
                (self.directory / "output").write_text(request.path)
            manifest = artifact_manifest(self.directory)
            return {
                "text": "fixture evidence",
                "review_passed": True,
                "artifact_stable": True,
                "artifacts": {"before": manifest, "after": manifest},
                "checks": [{"command": value, "exit_code": 0} for value in request.node.checks],
            }

    monkeypatch.setattr("myself_agent.orchestration_runtime.DeepAgentRunner", Runner)
    return visits


def paused(tmp_path, *, calls=24, extra=()):
    store, run, workspace, session = new_workflow(
        tmp_path, [work(access="write"), review(), human(), *extra], calls=calls
    )
    result = run_orchestration(store, run["id"])
    assert result["status"] == "waiting_human"
    checkpoint = store.recovery.view(run["id"])["checkpoints"][-1]
    body = {
        "request_id": uuid.uuid4().hex,
        "checkpoint_id": checkpoint["id"],
        "digest": checkpoint["digest"],
        "decision": "approve",
        "note": "Reviewed exact artifact",
    }
    return store, run, workspace, session, checkpoint, body


def approve_and_run(store, run, body):
    record, schedule = store.recovery.decide(run["id"], body, actor="test-operator")
    assert schedule
    store.status(run["id"], "running")
    return record, run_orchestration(store, run["id"])


def test_pause_resume_never_replays_and_preserves_all_budgets(tmp_path, fixture_runner):
    store, run, _, _, checkpoint, body = paused(
        tmp_path, extra=[work("after", depends_on=["human"]), review("final", ("after",))]
    )
    assert fixture_runner == ["root/build", "root/review"]
    assert checkpoint["payload"]["runtime"]["model_calls"] == 2
    assert checkpoint["payload"]["steps"] == 3
    record, result = approve_and_run(store, run, body)
    assert result["passed"] is True
    assert result["steps"] == 5
    assert result["model_calls"] == 4
    assert len(result["usage"]) == 4
    assert result["human_decision_ids"] == [record["request_id"]]
    assert fixture_runner == ["root/build", "root/review", "root/after", "root/final"]
    tasks = {task["path"]: task for task in store.orchestration.tasks(run["id"])}
    assert tasks["root/human"]["status"] == "succeeded"
    # No re-queue even when the previous decision is returned after completion.
    repeated, schedule = store.recovery.decide(run["id"], body, actor="test-operator")
    assert repeated == record and not schedule


def test_second_gate_keeps_external_and_model_accounting(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(
        tmp_path,
        extra=[
            work("after", depends_on=["human"]),
            review("final", ("after",)),
            human("human2", "final"),
        ],
    )
    _, result = approve_and_run(store, run, body)
    assert result["status"] == "waiting_human"
    checkpoint = store.recovery.view(run["id"])["checkpoints"][-1]
    runtime = checkpoint["payload"]["runtime"]
    assert runtime["model_calls"] == runtime["mcp_calls"] == runtime["web_requests"] == 4
    assert len(runtime["usage"]) == 4
    assert checkpoint["payload"]["steps"] == 6


def test_waiting_reserves_workspace_and_survives_manager_restart(tmp_path, fixture_runner):
    store, run, workspace, _, _, _ = paused(tmp_path)
    other_session = store.create_session(workspace["id"], "other")
    with pytest.raises(BusyError, match="reserved"):
        store.submit(other_session["id"], uuid.uuid4().hex, "must not write")
    manager = AgentManager(tmp_path, tmp_path / "absent-config")
    try:
        assert manager.store.run(run["id"])["status"] == "waiting_human"
    finally:
        manager.close()
    assert store.run(run["id"])["status"] == "waiting_human"


@pytest.mark.parametrize("change", ["output", ".git/config"])
def test_edit_after_pause_invalidates_approval(tmp_path, fixture_runner, change):
    store, run, workspace, _, _, body = paused(tmp_path)
    target = store.directory(workspace["id"]) / change
    target.parent.mkdir(exist_ok=True)
    target.write_text("intervening edit")
    with pytest.raises(ConfigurationError, match="changed"):
        store.recovery.decide(run["id"], body, actor="test-operator")
    assert not store.recovery.view(run["id"])["decisions"]
    assert store.run(run["id"])["status"] == "waiting_human"


def test_changed_after_approval_refuses_worker(tmp_path, fixture_runner):
    store, run, workspace, _, _, body = paused(tmp_path)
    store.recovery.decide(run["id"], body, actor="test-operator")
    (store.directory(workspace["id"]) / "output").write_text("changed")
    store.status(run["id"], "running")
    with pytest.raises(ConfigurationError, match="changed"):
        run_orchestration(store, run["id"])
    assert fixture_runner == ["root/build", "root/review"]


def test_cancel_is_terminal_for_decisions(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(tmp_path)
    assert store.orchestration.stop(run["id"], "cancelled", "UserStopped")
    with pytest.raises(BusyError, match="stale"):
        store.recovery.decide(run["id"], body, actor="test-operator")
    assert store.recovery.view(run["id"])["checkpoints"][0]["state"] == "cancelled"


def test_human_reject_never_executes_downstream(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(tmp_path, extra=[work("after", depends_on=["human"])])
    body["decision"] = "reject"
    _, schedule = store.recovery.decide(run["id"], body, actor="test-operator")
    assert not schedule
    assert store.run(run["id"])["status"] == "failed"
    assert store.orchestration.run(run["id"])["result"]["reason"] == "HumanRejected"
    assert fixture_runner == ["root/build", "root/review"]


def test_decision_conflict_and_bad_digest_are_denied(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(tmp_path)
    with pytest.raises(BusyError):
        store.recovery.decide(run["id"], body | {"digest": "0" * 64}, actor="test-operator")
    store.recovery.decide(run["id"], body, actor="test-operator")
    for altered in (body | {"note": "different"}, body | {"decision": "reject"}):
        with pytest.raises(BusyError, match="different"):
            store.recovery.decide(run["id"], altered, actor="test-operator")


def test_continuation_does_not_reset_model_limit(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(
        tmp_path, calls=2, extra=[work("after", depends_on=["human"])]
    )
    with pytest.raises(ConfigurationError, match="budget"):
        approve_and_run(store, run, body)
    assert fixture_runner == ["root/build", "root/review"]


def test_frozen_definition_change_refuses_resume(tmp_path, fixture_runner):
    store, run, _, _, _, body = paused(tmp_path)
    store.recovery.decide(run["id"], body, actor="test-operator")
    snapshot = store.orchestration.snapshot(run["id"])
    snapshot["workflow"]["name"] = "unapproved change"
    with store.connect() as db:
        db.execute(
            "UPDATE orchestrations SET snapshot=? WHERE run_id=?", (json.dumps(snapshot), run["id"])
        )
    store.status(run["id"], "running")
    with pytest.raises(ConfigurationError, match="definition"):
        run_orchestration(store, run["id"])


@pytest.mark.docker
@pytest.mark.parametrize("answer", [42, 0])
def test_real_docker_human_pause_never_overrides_commands(tmp_path, settings, answer):
    command = (
        'python -I -c "from pathlib import Path; '
        "assert Path('/workspace/output').read_text()=='42'\""
    )
    store, run, workspace, _ = new_workflow(
        tmp_path,
        [
            work(access="write"),
            review(checks=[command]),
            human(),
        ],
    )
    seen = []

    @contextmanager
    def factory(request):
        seen.append(request.path)
        responses = [AIMessage(content='{"passed":true,"summary":"fixture review"}')]
        if request.node.kind == "agent":
            responses = [
                call("write_file", {"file_path": "/workspace/output", "content": str(answer)}),
                AIMessage(content="fixture output written"),
            ]
        yield ScriptModel(responses=responses)

    result = run_orchestration(store, run["id"], model_factory=factory, image=settings.image)
    if answer == 0:
        assert result["passed"] is False
        assert not store.recovery.view(run["id"])["checkpoints"]
        assert (store.directory(workspace["id"]) / "output").read_text() == "0"
        return
    assert result["status"] == "waiting_human"
    checkpoint = store.recovery.view(run["id"])["checkpoints"][-1]
    assert checkpoint["payload"]["runtime"]["model_calls"] == 3
    store.recovery.decide(
        run["id"],
        {
            "request_id": uuid.uuid4().hex,
            "checkpoint_id": checkpoint["id"],
            "digest": checkpoint["digest"],
            "decision": "approve",
            "note": "human reviewed real Docker evidence",
        },
        actor="test",
    )
    store.status(run["id"], "running")
    result = run_orchestration(store, run["id"], model_factory=factory, image=settings.image)
    assert result["passed"] is True and result["model_calls"] == 3
    assert seen == ["root/build", "root/review"]


def test_human_api_actor_origin_and_idempotent_queue(tmp_path, fixture_runner):
    store, run, _, _, checkpoint, body = paused(tmp_path)
    with TestClient(create_app(tmp_path, manager_factory=QueuedManager)) as client:
        base = "/next/api/orchestration/runs/" + run["id"]
        assert client.get(base + "/checkpoints").json()["checkpoints"][0]["id"] == checkpoint["id"]
        assert (
            client.post(
                base + "/decisions", json=body, headers={"origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.post(base + "/decisions", json=body | {"actor": "spoof"}).status_code == 422
        accepted = client.post(base + "/decisions", json=body)
        assert accepted.status_code == 202
        assert accepted.json()["decision"]["actor"] == "authenticated-shared-operator"
        assert client.app.state.manager.submissions == [run["id"]]
        assert client.post(base + "/decisions", json=body).status_code == 202
        assert client.app.state.manager.submissions == [run["id"]]


@pytest.mark.parametrize(
    "change",
    [
        {"source": None},
        {"source": "build"},
        {"depends_on": []},
        {"access": "write"},
        {"prompt": "ignored"},
        {"checks": ["true"]},
        {"required_skills": ["build"]},
        {"max_iterations": 1},
    ],
)
def test_human_node_contract_rejects_ambiguous_or_unsafe_fields(change):
    with pytest.raises(ValidationError):
        WorkflowInput(
            name="invalid",
            flow=Flow(nodes=[work(), review(), Node.model_validate(human().model_dump() | change)]),
        )


def test_nested_human_gate_is_rejected_but_normal_nested_flow_allowed():
    with pytest.raises(ValidationError, match="top-level"):
        WorkflowInput(
            name="nested",
            flow=Flow(
                nodes=[
                    Node(id="sub", kind="subworkflow", body=Flow(nodes=[work(), review(), human()]))
                ]
            ),
        )
    WorkflowInput(
        name="nested",
        flow=Flow(nodes=[Node(id="sub", kind="subworkflow", body=Flow(nodes=[work(), review()]))]),
    )


def test_unknown_checkpoint_version_is_not_reinterpreted(tmp_path, fixture_runner):
    store, run, _, _, checkpoint, body = paused(tmp_path)
    payload = checkpoint["payload"] | {"format_version": 999}
    with store.connect() as db:
        db.execute(
            "UPDATE human_checkpoints SET payload=? WHERE id=?",
            (json.dumps(payload), checkpoint["id"]),
        )
    store.recovery.decide(run["id"], body, actor="test-operator")
    store.status(run["id"], "running")
    with pytest.raises(ConfigurationError, match="format version"):
        run_orchestration(store, run["id"])
    assert fixture_runner == ["root/build", "root/review"]
