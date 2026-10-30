"""Offline execution-semantics tests. Runner results here are explicit fixtures."""

import json
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest

from myself_agent.general_settings import ConfigurationError, PolicyInput
from myself_agent.general_store import AgentStore
from myself_agent.orchestration_engine import OrchestrationStopped, Orchestrator
from myself_agent.orchestration_models import AgentInput, Flow, Node, TeamInput, WorkflowInput


def work(identifier="build", **kwargs):
    return Node.model_validate(
        {"id": identifier, "prompt": identifier, "required_skills": ["build"]} | kwargs
    )


def review(identifier="review", dependencies=("build",), **kwargs):
    return Node.model_validate(
        {
            "id": identifier,
            "kind": "verify",
            "prompt": "independently check",
            "required_skills": ["test"],
            "depends_on": list(dependencies),
            "checks": ["python -V"],
        }
        | kwargs
    )


def new_workflow(root, nodes, *, parallel=2, max_steps=48, calls=24):
    store = AgentStore(root)
    workspace = store.create_workspace("orchestration")
    session = store.create_session(workspace["id"], "workflow")
    store.config.save_policy("session:" + session["id"], PolicyInput(max_model_calls=calls))
    for identifier in ("worker-a", "worker-b"):
        store.orchestration.save(
            "agent",
            AgentInput(
                id=identifier,
                name=identifier,
                skills=["build", "plan"],
                tools=["read_file", "write_file", "edit_file", "execute"],
            ),
        )
    store.orchestration.save(
        "agent",
        AgentInput(
            id="reviewer",
            name="independent",
            role="verifier",
            skills=["test"],
            tools=["read_file", "execute"],
        ),
    )
    store.orchestration.save(
        "team",
        TeamInput(
            id="team",
            name="team",
            agent_ids=["worker-a", "worker-b", "reviewer"],
            max_parallel=parallel,
        ),
    )
    store.orchestration.save(
        "workflow",
        WorkflowInput(id="flow", name="flow", flow=Flow(nodes=nodes), max_steps=max_steps),
    )
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "off")
    run, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "complete this arbitrary task",
        reasoning_effort="off",
        action="orchestrate",
        orchestration=snapshot,
    )
    store.status(run["id"], "running")
    return store, run, workspace, session


def fixture_outcome(request, passed=True):
    return {
        "text": "offline fixture result",
        "review_passed": passed,
        "artifact_stable": True,
        "artifacts": {"before": {"sha256": "a" * 64}, "after": {"sha256": "a" * 64}},
        "checks": [
            {"command": command, "exit_code": 0 if passed else 1, "output": "offline fixture"}
            for command in request.node.checks
        ],
    }


def test_sequence_persists_independent_assignment_and_evidence(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    requests = []

    def runner(request):
        requests.append(request)
        return fixture_outcome(request)

    result = Orchestrator(store, run["id"], runner).run()
    assert result["passed"] is True
    assert [r.node.id for r in requests] == ["build", "review"]
    assert requests[0].agent["id"] != requests[1].agent["id"]
    assert requests[0].path in result["verified_paths"]
    restarted = AgentStore(tmp_path)
    persisted = restarted.orchestration.run(run["id"])
    assert persisted["result"]["passed"] is True
    assert all(t["status"] == "succeeded" for t in persisted["tasks"])
    events = restarted.events(run["session_id"])
    decision = next(e for e in events if e["kind"] == "orchestration.scheduled")
    assert {c["reason"] for c in decision["payload"]["candidates"]} == {None, "role"}


def test_parallel_read_branches_use_distinct_available_agents(tmp_path):
    store, run, _, _ = new_workflow(
        tmp_path, [work("alpha"), work("beta"), review(dependencies=("alpha", "beta"))]
    )
    barrier = threading.Barrier(2)
    actors = []

    def runner(request):
        if request.node.kind == "agent":
            actors.append(request.agent["id"])
            barrier.wait(timeout=5)
        return fixture_outcome(request)

    result = Orchestrator(store, run["id"], runner).run()
    assert result["passed"] is True and len(set(actors)) == 2


def test_writes_are_workspace_exclusive_even_with_parallel_team(tmp_path):
    store, run, _, _ = new_workflow(
        tmp_path,
        [
            work("alpha", access="write"),
            work("beta", access="write"),
            review(dependencies=("alpha", "beta")),
        ],
    )
    lock, active, maximum = threading.Lock(), 0, 0

    def runner(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return fixture_outcome(request)

    assert Orchestrator(store, run["id"], runner).run()["passed"] is True
    assert maximum == 1


def test_priority_and_downstream_readiness_affect_dispatch(tmp_path):
    store, run, _, _ = new_workflow(
        tmp_path,
        [work("low"), work("high", priority=10), review(dependencies=("low", "high"))],
        parallel=1,
    )
    order = []

    def runner(request):
        order.append(request.node.id)
        return fixture_outcome(request)

    assert Orchestrator(store, run["id"], runner).run()["passed"] is True
    assert order == ["high", "low", "review"]


def test_feedback_loop_reuses_unaffected_plan_and_reassigns_repair(tmp_path):
    body = Flow(
        nodes=[
            work("plan", required_skills=["plan"]),
            work(depends_on=["plan"], access="write"),
            review(repair_targets=["build"]),
        ]
    )
    store, run, _, _ = new_workflow(
        tmp_path, [Node(id="repair", kind="loop", body=body, until="review", max_iterations=3)]
    )
    calls, builders = Counter(), []

    def runner(request):
        calls[request.node.id] += 1
        if request.node.id == "build":
            builders.append(request.agent["id"])
            if calls["build"] == 2:
                assert request.feedback["rejections"]
        result = fixture_outcome(request)
        if request.node.kind == "verify" and calls["review"] == 1:
            result["checks"][0]["exit_code"] = 1  # Model says pass; hard evidence wins.
        return result

    result = Orchestrator(store, run["id"], runner).run()
    assert result["passed"] is True
    assert calls == {"plan": 1, "build": 2, "review": 2}
    assert len(set(builders)) == 2
    tasks = store.orchestration.tasks(run["id"])
    assert any(t["status"] == "rejected" and t["kind"] == "verify" for t in tasks)
    reused = next(t for t in tasks if t["status"] == "reused")
    assert reused["result"]["reused_from"] == "root/repair/iteration-1/plan"
    assert all("iteration-2" in v for v in result["verification_ids"])


def test_loop_exhaustion_blocks_downstream_and_is_not_success(tmp_path):
    body = Flow(nodes=[work(), review(repair_targets=["build"])])
    store, run, _, _ = new_workflow(
        tmp_path,
        [
            Node(id="repair", kind="loop", body=body, until="review", max_iterations=2),
            work("after", depends_on=["repair"]),
            review("final", dependencies=("after",)),
        ],
    )
    calls = []

    def runner(request):
        calls.append(request.path)
        return fixture_outcome(request, passed=False)

    result = Orchestrator(store, run["id"], runner).run()
    assert result["passed"] is False and result["status"] == "rejected"
    assert len(calls) == 4 and not any("after" in p for p in calls)
    assert any(t["status"] == "skipped" for t in store.orchestration.tasks(run["id"]))


@pytest.mark.parametrize("initial_pass", [True, False])
def test_condition_selects_only_verified_branch_and_can_repair(initial_pass, tmp_path):
    branches = {
        name: Flow(nodes=[work(name, access="write"), review(dependencies=(name,))])
        for name in ("keep", "fix")
    }
    choice = Node(
        id="choose",
        kind="condition",
        source="review",
        depends_on=["review"],
        on_true=branches["keep"],
        on_false=branches["fix"],
    )
    store, run, _, _ = new_workflow(tmp_path, [work(), review(), choice])
    paths = []

    def runner(request):
        paths.append(request.path)
        return fixture_outcome(request, initial_pass if request.path == "root/review" else True)

    result = Orchestrator(store, run["id"], runner).run()
    assert result["passed"] is True
    assert any(("on_true" if initial_pass else "on_false") in p for p in paths)
    assert not any(("on_false" if initial_pass else "on_true") in p for p in paths)


def test_nested_subworkflow_carries_dependency_evidence(tmp_path):
    inner = Node(id="inner", kind="subworkflow", body=Flow(nodes=[work(), review()]))
    outer = Node(id="outer", kind="subworkflow", depends_on=["plan"], body=Flow(nodes=[inner]))
    store, run, _, _ = new_workflow(tmp_path, [work("plan"), outer])
    result = Orchestrator(store, run["id"], fixture_outcome).run()
    assert result["passed"] is True
    assert "root/plan" in result["verified_paths"]
    assert "root/outer/inner/review" in result["verification_ids"]


@pytest.mark.parametrize("mutation", ["review", "exit", "command", "missing", "artifacts"])
def test_verification_requires_all_independent_evidence(tmp_path, mutation):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])

    def runner(request):
        result = fixture_outcome(request)
        if request.node.kind == "verify":
            if mutation == "review":
                result["review_passed"] = False
            elif mutation == "exit":
                result["checks"][0]["exit_code"] = 1
            elif mutation == "command":
                result["checks"][0]["command"] = "different"
            elif mutation == "missing":
                result["checks"] = []
            else:
                result["artifact_stable"] = False
        return result

    assert Orchestrator(store, run["id"], runner).run()["passed"] is False


def test_unverified_parallel_output_cannot_be_silently_accepted(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), work("uncovered"), review()])
    result = Orchestrator(store, run["id"], fixture_outcome).run()
    assert result["passed"] is not True
    assert "root/uncovered" not in result["verified_paths"]


def test_step_budget_and_failure_are_durable(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()], max_steps=1)
    with pytest.raises(ConfigurationError, match="step budget"):
        Orchestrator(store, run["id"], fixture_outcome).run()
    assert store.orchestration.run(run["id"])["result"]["status"] == "interrupted"
    assert not any(t["status"] == "running" for t in store.orchestration.tasks(run["id"]))


def test_cancelled_run_starts_no_attempts(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    store.status(run["id"], "cancelled")
    with pytest.raises(OrchestrationStopped):
        Orchestrator(store, run["id"], lambda r: pytest.fail("must not run")).run()
    assert not store.orchestration.tasks(run["id"])


def test_worker_error_does_not_leak_exception_text(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])

    def runner(request):
        raise RuntimeError("private-value-must-not-appear")

    with pytest.raises(RuntimeError):
        Orchestrator(store, run["id"], runner).run()
    assert "private-value" not in json.dumps(store.orchestration.run(run["id"]))


def test_documented_example_is_a_runnable_declarative_fixture(tmp_path):
    document = json.loads(
        (Path(__file__).parents[1] / "examples" / "orchestration.json").read_text()
    )
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("example")
    session = store.create_session(workspace["id"], "example")
    for member in document["agents"]:
        store.orchestration.save("agent", AgentInput.model_validate(member))
    store.orchestration.save("team", TeamInput.model_validate(document["team"]))
    store.orchestration.save("workflow", WorkflowInput.model_validate(document["workflow"]))
    snapshot = store.orchestration.prepare(
        session["id"], document["team"]["id"], document["workflow"]["id"], "off"
    )
    run, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "documented example",
        reasoning_effort="off",
        action="orchestrate",
        orchestration=snapshot,
    )
    store.status(run["id"], "running")
    result = Orchestrator(store, run["id"], fixture_outcome).run()
    assert result["passed"] is True
    assert {task["kind"] for task in store.orchestration.tasks(run["id"])} == {
        "agent",
        "verify",
        "loop",
        "condition",
        "subworkflow",
    }
