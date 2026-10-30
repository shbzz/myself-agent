import json
import sqlite3
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from test_orchestration_engine import new_workflow, review, work

from myself_agent.general_manager import AgentManager
from myself_agent.general_settings import ConfigurationError, ModelInput, PolicyInput, ProviderInput
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError
from myself_agent.orchestration_models import AgentInput, Flow, TeamInput, WorkflowInput


class QueuedManager(AgentManager):
    def __init__(self, root, config):
        self.store = AgentStore(root)
        self.lock = threading.Lock()
        self.processes = {}
        self.submissions = []
        outer = self

        class Queue:
            def submit(self, function, run_id):
                outer.submissions.append(run_id)

        self.pool = Queue()

    @staticmethod
    def cleanup(run_id):
        pass

    def close(self):
        pass


def definitions(client):
    base = "/next/api/orchestration"
    for body in [
        {
            "id": "author",
            "name": "Author",
            "skills": ["build"],
            "tools": ["read_file", "write_file"],
        },
        {
            "id": "reviewer",
            "name": "Reviewer",
            "role": "verifier",
            "skills": ["test"],
            "tools": ["read_file"],
        },
    ]:
        assert client.post(base + "/agents", json=body).status_code == 201
    assert (
        client.post(
            base + "/teams",
            json={"id": "team", "name": "team", "agent_ids": ["author", "reviewer"]},
        ).status_code
        == 201
    )
    flow = WorkflowInput(id="flow", name="flow", flow=Flow(nodes=[work(), review()]))
    assert client.post(base + "/workflows", json=flow.model_dump()).status_code == 201
    workspace = client.post("/next/api/workspaces", json={"name": "backend-only"}).json()
    session = client.post(
        "/next/api/sessions", json={"workspace_id": workspace["id"], "title": "api"}
    ).json()
    return {
        "session_id": session["id"],
        "request_id": uuid.uuid4().hex,
        "text": "A free-form complex task",
        "team_id": "team",
        "workflow_id": "flow",
        "reasoning_effort": "off",
    }


def test_registry_discovery_versions_and_frozen_private_configuration(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=QueuedManager)) as client:
        body = definitions(client)
        response = client.post("/next/api/orchestration/runs", json=body)
        assert response.status_code == 202
        identifier = response.json()["id"]
        before = client.get(f"/next/api/orchestration/runs/{identifier}").json()
        update = {
            "id": "author",
            "name": "Disabled",
            "skills": ["other"],
            "tools": [],
            "enabled": False,
        }
        assert client.post("/next/api/orchestration/agents", json=update).status_code == 201
        versions = client.get("/next/api/orchestration/agents/author/versions").json()["versions"]
        assert [v["version"] for v in versions] == [2, 1]
        assert client.get("/next/api/orchestration/agents?skill=build").json()["agents"] == []
        assert len(client.get("/next/api/orchestration/agents?enabled=false").json()["agents"]) == 1
        after = client.get(f"/next/api/orchestration/runs/{identifier}")
        assert after.json()["snapshot"] == before["snapshot"]
        assert "credential_ref" not in after.text and "builtin:deepseek" not in after.text
        assert client.post("/next/api/orchestration/runs", json=body).json()["id"] == identifier
        assert len(client.app.state.manager.submissions) == 1
        conflict = client.post("/next/api/orchestration/runs", json=body | {"workflow_id": "other"})
        assert conflict.status_code == 409
        assert (
            len(
                client.get(
                    "/next/api/orchestration/runs", params={"session_id": body["session_id"]}
                ).json()["runs"]
            )
            == 1
        )
        assert (
            client.post(f"/next/api/orchestration/runs/{identifier}/cancel", json={}).json()[
                "status"
            ]
            == "cancelled"
        )
        assert (
            client.get(f"/next/api/orchestration/runs/{identifier}").json()["result"]["status"]
            == "cancelled"
        )
        events = client.get(f"/next/api/orchestration/runs/{identifier}/events").json()["events"]
        assert [e["kind"] for e in events] == ["run.queued", "run.cancelled"]
        assert (
            client.get(
                f"/next/api/orchestration/runs/{identifier}/events",
                params={"after": events[-1]["sequence"]},
            ).json()["events"]
            == []
        )


def test_registry_errors_still_use_existing_api_security_boundary(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=QueuedManager)) as client:
        body = definitions(client)
        base = "/next/api/orchestration"
        assert (
            client.post(
                base + "/runs", json=body, headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                base + "/runs", content="{}", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            client.post(
                base + "/workflows",
                content='{"name":"' + "x" * 70000 + '"}',
                headers={"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert client.get(base + "/runs/unknown").status_code == 404
        assert (
            client.post(
                base + "/teams", json={"name": "bad", "agent_ids": ["missing", "absent"]}
            ).status_code
            == 404
        )
        bad = client.post(
            base + "/agents", json={"name": "bad", "skills": ["build"], "api_key": "must-not-echo"}
        )
        assert bad.status_code == 422 and "must-not-echo" not in bad.text


def test_submission_is_atomic_if_snapshot_insert_fails(tmp_path):
    store, run, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(run["id"], "succeeded")
    snapshot = store.orchestration.snapshot(run["id"])
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_snapshot BEFORE INSERT ON orchestrations "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    request = uuid.uuid4().hex
    with pytest.raises(sqlite3.IntegrityError):
        store.submit(
            session["id"],
            request,
            "must roll back",
            reasoning_effort="off",
            action="orchestrate",
            orchestration=snapshot,
        )
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM runs WHERE request_id=?", (request,)).fetchone()[0]
            == 0
        )
    assert len(store.runs(session["id"])) == 1


def test_orchestration_and_regular_sessions_share_global_run_lease(tmp_path):
    store, run, _, session = new_workflow(tmp_path, [work(), review()])
    with pytest.raises(BusyError):
        store.submit(session["id"], uuid.uuid4().hex, "normal message", reasoning_effort="off")
    with pytest.raises(ConfigurationError):
        store.submit(session["id"], uuid.uuid4().hex, "no snapshot", action="orchestrate")
    with pytest.raises(ConfigurationError):
        store.submit(
            session["id"],
            uuid.uuid4().hex,
            "wrong action",
            orchestration=store.orchestration.snapshot(run["id"]),
        )


@pytest.mark.parametrize("nodes", [[work()], [review(dependencies=())]])
def test_single_agent_or_unverified_only_workflow_is_not_a_runnable_team(tmp_path, nodes):
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("invalid")
    session = store.create_session(workspace["id"], "invalid")
    for identifier, role, skill in [
        ("author", "worker", "build"),
        ("reviewer", "verifier", "test"),
    ]:
        store.orchestration.save(
            "agent", AgentInput(id=identifier, name=identifier, role=role, skills=[skill])
        )
    store.orchestration.save(
        "team", TeamInput(id="team", name="team", agent_ids=["author", "reviewer"])
    )
    store.orchestration.save(
        "workflow", WorkflowInput(id="flow", name="flow", flow=Flow(nodes=nodes))
    )
    with pytest.raises(ConfigurationError, match="both execution"):
        store.orchestration.prepare(session["id"], "team", "flow", "off")


def test_private_role_credentials_are_versioned_and_never_returned(tmp_path):
    store, run, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(run["id"], "succeeded")
    provider = store.config.save_provider(
        ProviderInput(
            id="custom",
            name="custom",
            protocol="openai_compatible",
            base_url="http://127.0.0.1:9001/v1",
            api_key="fixture-role-key",
        )
    )
    store.config.save_model(
        ModelInput(
            id="custom-model",
            name="custom",
            provider_id=provider["id"],
            model_id="custom-model",
            context_window_tokens=131072,
            max_output_tokens=8192,
            tools=True,
            streaming=False,
        )
    )
    store.orchestration.save(
        "agent",
        AgentInput(
            id="worker-a",
            name="custom",
            skills=["build"],
            tools=["read_file"],
            model_profile_id="custom-model",
        ),
    )
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "off")
    run2, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "custom model task",
        reasoning_effort="off",
        action="orchestrate",
        orchestration=snapshot,
    )
    private = store.orchestration.snapshot(run2["id"])["agents"][0]["configuration"]["provider"]
    reference = private["credential_ref"]
    store.config.save_provider(
        ProviderInput(
            id="custom",
            name="changed",
            protocol="openai_compatible",
            base_url="http://127.0.0.1:9001/v1",
            api_key="different-role-key",
        )
    )
    assert (
        store.orchestration.snapshot(run2["id"])["agents"][0]["configuration"]["provider"][
            "credential_ref"
        ]
        == reference
    )
    public = json.dumps(store.orchestration.run(run2["id"]))
    assert (
        "credential_ref" not in public
        and "fixture-role-key" not in public
        and reference not in public
    )


def test_cancellation_is_terminal_against_late_evidence_writes(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    store.orchestration.task(run["id"], "root/build", work(), "running", agent_id="worker-a")
    store.status(run["id"], "cancelled", "UserStopped")
    store.orchestration.interrupt(run["id"], "cancelled", "UserStopped")
    assert store.orchestration.finish(run["id"], {"status": "succeeded"}) is False
    store.orchestration.task(run["id"], "root/build", work(), "succeeded", result={"passed": True})
    store.orchestration.interrupt(run["id"], "interrupted", "LateWorkerException")
    value = store.orchestration.run(run["id"])
    assert value["tasks"][0]["status"] == "cancelled"
    assert value["result"] == {"status": "cancelled", "reason": "UserStopped"}


def test_disabled_member_does_not_resolve_or_grant_its_model(tmp_path):
    store, run, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(run["id"], "succeeded")
    store.orchestration.save(
        "agent", AgentInput(id="worker-b", name="disabled", skills=["build"], enabled=False)
    )
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "off")
    disabled = next(a for a in snapshot["agents"] if a["definition"]["id"] == "worker-b")
    assert disabled["configuration"] is None
    second, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "disabled member",
        reasoning_effort="off",
        action="orchestrate",
        orchestration=snapshot,
    )
    assert (
        next(
            a
            for a in store.orchestration.snapshot(second["id"], public=True)["agents"]
            if a["definition"]["id"] == "worker-b"
        )["configuration"]
        is None
    )


def test_role_models_not_unused_default_determine_supported_effort(tmp_path):
    store, baseline, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(baseline["id"], "succeeded")
    provider = store.config.save_provider(
        ProviderInput(
            id="off-only",
            name="off-only",
            protocol="openai_compatible",
            base_url="http://127.0.0.1:9001/v1",
        )
    )
    store.config.save_model(
        ModelInput(
            id="off-only",
            name="off-only",
            provider_id=provider["id"],
            model_id="off-only",
            context_window_tokens=131072,
            max_output_tokens=8192,
        )
    )
    store.config.save_policy("session:" + session["id"], PolicyInput(model_profile_id="off-only"))
    for member in store.config.list("agent"):
        value = {key: val for key, val in member.items() if key != "version"}
        value["model_profile_id"] = "deepseek-flash"
        store.orchestration.save("agent", AgentInput.model_validate(value))
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "high")
    assert snapshot["configuration"]["profile"]["reasoning_levels"] == ["off"]
    run, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "independent role models",
        reasoning_effort="high",
        action="orchestrate",
        orchestration=snapshot,
    )
    assert store.run(run["id"])["reasoning_effort"] == "high"
    assert all(a["configuration"]["profile"]["id"] == "deepseek-flash" for a in snapshot["agents"])


def test_role_policy_provenance_stays_frozen_during_concurrent_edit(tmp_path, monkeypatch):
    store, run, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(run["id"], "succeeded")
    original = store.config.resolve
    edited = False

    def resolve(*args, **kwargs):
        nonlocal edited
        if kwargs.get("override") and not edited:
            edited = True
            policy = store.config.get("policy", "global")
            policy["max_web_requests"] = 1
            store.config.write("policy", "global", policy)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.config, "resolve", resolve)
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "off")
    assert edited
    assert store.config.get("policy", "global")["max_web_requests"] == 1
    frozen = snapshot["configuration"]
    for member in snapshot["agents"]:
        assert (
            member["configuration"]["policy"]["max_web_requests"]
            == frozen["policy"]["max_web_requests"]
            == 20
        )
        assert member["configuration"]["revisions"] == frozen["revisions"]
