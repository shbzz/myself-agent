import json
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_general import ScriptModel, call
from test_orchestration_engine import new_workflow, review, work

from myself_agent.general_context import CallBudget
from myself_agent.general_manager import AgentManager
from myself_agent.general_sandbox import WorkspaceSandbox
from myself_agent.general_settings import ConfigurationError
from myself_agent.orchestration_models import Flow, Node
from myself_agent.orchestration_runtime import artifact_manifest, run_orchestration


def test_artifact_inventory_binds_contents_and_refuses_links_and_oversize(tmp_path):
    (tmp_path / "artifact").write_text("one")
    before = artifact_manifest(tmp_path)
    (tmp_path / "artifact").write_text("two")
    assert artifact_manifest(tmp_path)["sha256"] != before["sha256"]
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(ConfigurationError, match="links"):
        artifact_manifest(tmp_path)
    (tmp_path / "link").unlink()
    with (tmp_path / "too-large").open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ConfigurationError, match="size"):
        artifact_manifest(tmp_path)


def test_global_model_call_budget_is_thread_safe():
    budget = CallBudget(3)

    def charge(_):
        try:
            budget.on_chat_model_start(None, None, run_id=uuid.uuid4())
            return True
        except ConfigurationError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(charge, range(100)))
    assert sum(accepted) == budget.calls == 3


@pytest.mark.docker
def test_real_deep_agents_and_docker_evidence_repair_loop(tmp_path, settings):
    command = (
        "python -I -c \"import runpy; m=runpy.run_path('/workspace/result.py'); "
        "assert m['answer']()==42; print('verified-42')\""
    )
    body = Flow(nodes=[work(access="write"), review(checks=[command], repair_targets=["build"])])
    store, run, workspace, _ = new_workflow(
        tmp_path,
        [Node(id="repair", kind="loop", body=body, until="review", max_iterations=2)],
        calls=12,
    )
    models = []

    @contextmanager
    def factory(request):
        first = "iteration-1" in request.path
        if request.node.kind == "agent":
            responses = (
                [
                    call(
                        "write_file",
                        {
                            "file_path": "/workspace/result.py",
                            "content": "def answer():\n    return 0\n",
                        },
                    )
                ]
                if first
                else [
                    call("read_file", {"file_path": "/workspace/result.py"}),
                    call(
                        "edit_file",
                        {
                            "file_path": "/workspace/result.py",
                            "old_string": "return 0",
                            "new_string": "return 42",
                        },
                    ),
                ]
            )
            responses.append(AIMessage(content="offline fixture author handoff"))
        else:
            responses = (
                [call("execute", {"command": "printf forbidden > /workspace/forbidden.txt"})]
                if first
                else [call("read_file", {"file_path": "/workspace/result.py"})]
            )
            responses.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "passed": True,
                            "summary": "offline reviewer claims pass; command has final authority",
                        }
                    )
                )
            )
        model = ScriptModel(responses=responses)
        models.append((request, model))
        yield model

    result = run_orchestration(store, run["id"], model_factory=factory, image=settings.image)
    assert result["passed"] is True and result["model_calls"] == 9
    assert "return 42" in (store.directory(workspace["id"]) / "result.py").read_text()
    assert not (store.directory(workspace["id"]) / "forbidden.txt").exists()
    tasks = store.orchestration.tasks(run["id"])
    rejected = next(t for t in tasks if t["kind"] == "verify" and t["status"] == "rejected")
    assert rejected["result"]["review_passed"] is True
    assert rejected["result"]["checks"][0]["exit_code"] != 0
    passed = [t for t in tasks if t["kind"] == "verify" and t["status"] == "succeeded"][0]
    assert "verified-42" in passed["result"]["checks"][0]["output"]
    assert passed["result"]["artifacts"]["before"] == passed["result"]["artifacts"]["after"]
    assert all("task" not in m.names for _, m in models)
    for request, model in models:
        if request.node.kind == "verify":
            assert "write_file" not in model.names and "edit_file" not in model.names
    assert all(request.node.prompt in str(model.seen[0][-1].content) for request, model in models)
    events = store.events(run["session_id"])
    assert any(e["kind"] == "tool.finished" and e["payload"]["failed"] for e in events)
    assert all("task_path" in e["payload"] for e in events if e["kind"] == "tool.started")
    listing = subprocess.check_output(
        ["sudo", "-n", "docker", "ps", "-aq", "--filter", "label=myself-agent.parent=" + run["id"]],
        text=True,
    )
    assert not listing.strip()


@pytest.mark.docker
def test_tool_scope_enforced_on_fabricated_tool_call(tmp_path, settings):
    command = "test ! -e /workspace/denied.txt"
    store, run, workspace, _ = new_workflow(tmp_path, [work(), review(checks=[command])])

    @contextmanager
    def factory(request):
        responses = (
            [
                call(
                    "write_file",
                    {"file_path": "/workspace/denied.txt", "content": "must not write"},
                ),
                AIMessage(content="done"),
            ]
            if request.node.kind == "agent"
            else [AIMessage(content='{"passed":true,"summary":"file absent"}')]
        )
        yield ScriptModel(responses=responses)

    assert (
        run_orchestration(store, run["id"], model_factory=factory, image=settings.image)["passed"]
        is True
    )
    assert not (store.directory(workspace["id"]) / "denied.txt").exists()
    assert any(e["kind"] == "tool.denied" for e in store.events(run["session_id"]))


@pytest.mark.docker
def test_shared_budget_stops_before_second_model_call(tmp_path, settings):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()], calls=1)
    model = ScriptModel(
        responses=[
            call("read_file", {"file_path": "/workspace/absent"}),
            AIMessage(content="must not be generated"),
        ]
    )

    @contextmanager
    def factory(request):
        yield model

    with pytest.raises(ConfigurationError, match="调用预算"):
        run_orchestration(store, run["id"], model_factory=factory, image=settings.image)
    assert len(model.seen) == 1 and len(model.responses) == 1
    assert not subprocess.check_output(
        ["sudo", "-n", "docker", "ps", "-aq", "--filter", "label=myself-agent.parent=" + run["id"]],
        text=True,
    ).strip()


@pytest.mark.docker
def test_supervisor_dispatch_cancel_and_restart_cleanup_labelled_containers(
    tmp_path, settings, monkeypatch
):
    store, baseline, workspace, session = new_workflow(tmp_path, [work(), review()])
    store.status(baseline["id"], "succeeded")
    commands = []
    original = subprocess.Popen

    def spawn(args, **kwargs):
        commands.append(args)
        return original(["python3", "-c", "import time; time.sleep(60)"], start_new_session=True)

    proxy = SimpleNamespace(
        Popen=spawn,
        run=subprocess.run,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr("myself_agent.general_manager.subprocess", proxy)
    manager = AgentManager(tmp_path, tmp_path / "unused.env")
    run = manager.submit_workflow(
        session["id"], uuid.uuid4().hex, "supervised task", "team", "flow"
    )
    for _ in range(100):
        if manager.store.run(run["id"])["status"] == "running":
            break
        time.sleep(0.01)
    try:
        assert "myself_agent.orchestration_runtime" in commands[0]
        with WorkspaceSandbox(
            store.directory(workspace["id"]),
            settings.image,
            uuid.uuid4().hex,
            parent_run_id=run["id"],
        ):
            assert manager.cancel(run["id"])["status"] == "cancelled"
            assert not subprocess.check_output(
                [
                    "sudo",
                    "-n",
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=myself-agent.parent=" + run["id"],
                ],
                text=True,
            ).strip()
    finally:
        manager.close()
    snapshot = store.orchestration.prepare(session["id"], "team", "flow", "off")
    pending, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        "hard restart",
        reasoning_effort="off",
        action="orchestrate",
        orchestration=snapshot,
    )
    store.orchestration.task(
        pending["id"],
        "root/build",
        work(),
        "running",
        agent_id="worker-a",
        attempt_id=uuid.uuid4().hex,
    )
    restarted = AgentManager(tmp_path, tmp_path / "unused.env")
    try:
        assert restarted.store.run(pending["id"])["status"] == "interrupted"
        assert restarted.store.orchestration.tasks(pending["id"])[0]["status"] == "interrupted"
        assert (
            restarted.store.orchestration.run(pending["id"])["result"]["reason"] == "ServerRestart"
        )
    finally:
        restarted.close()


def test_attempts_share_web_and_mcp_operation_budgets(tmp_path, monkeypatch):
    from myself_agent.external_tools import external_tools
    from myself_agent.mcp_tools import MCPAccess
    from myself_agent.web_tools import WebAccess

    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    config = store.run_configuration(run["id"])
    config["policy"].update(web_enabled=True, allowed_web_hosts=["example.com"], max_web_requests=1)
    server = {"id": "fixture-server", "allowed_tools": ["add"], "credential_ref": None}
    config["mcp_servers"] = [server]
    web = WebAccess(config["policy"], None)
    mcp = MCPAccess(store, [server], None, 5, max_calls=1)
    monkeypatch.setattr(web, "resolve", lambda *args: "93.184.216.34")
    monkeypatch.setattr(
        web, "exchange", lambda *args: (200, {"content-type": "text/plain"}, b"fixture page")
    )

    async def request(*args):
        return {"server_id": "fixture-server", "tools": []}

    monkeypatch.setattr(mcp, "request", request)
    first = {
        t.name: t for t in external_tools(store, run["id"], config, web_access=web, mcp_access=mcp)
    }
    second = {
        t.name: t for t in external_tools(store, run["id"], config, web_access=web, mcp_access=mcp)
    }
    assert "fixture page" in first["web_read"].invoke({"url": "https://example.com"})
    assert "额度" in second["web_read"].invoke({"url": "https://example.com"})
    assert '"tools": []' in first["mcp_list_tools"].invoke({"server_id": "fixture-server"})
    assert "额度" in second["mcp_list_tools"].invoke({"server_id": "fixture-server"})
    assert web.requests == mcp.calls == 1


def test_supervisor_spawn_failure_releases_the_active_run(tmp_path, monkeypatch):
    store, baseline, _, session = new_workflow(tmp_path, [work(), review()])
    store.status(baseline["id"], "succeeded")

    def fail(*args, **kwargs):
        raise OSError("private-diagnostic-not-for-api")

    proxy = SimpleNamespace(
        Popen=fail,
        run=subprocess.run,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr("myself_agent.general_manager.subprocess", proxy)
    manager = AgentManager(tmp_path, tmp_path / "unused.env")
    try:
        run = manager.submit_workflow(
            session["id"], uuid.uuid4().hex, "spawn failure", "team", "flow"
        )
        for _ in range(200):
            if manager.store.run(run["id"])["status"] == "failed":
                break
            time.sleep(0.01)
        value = manager.store.orchestration.run(run["id"])
        assert value["run"]["status"] == "failed"
        assert value["result"]["reason"] == "OSError"
        assert "private-diagnostic" not in json.dumps(value)
    finally:
        manager.close()


def test_runner_does_not_read_unused_default_provider_credential(tmp_path, monkeypatch):
    from myself_agent.config import DEFAULT_IMAGE
    from myself_agent.orchestration_runtime import DeepAgentRunner

    store, run, _, _ = new_workflow(tmp_path, [work(), review()])

    def unexpected(*args):
        pytest.fail("Runner construction must not read an unused provider credential")

    monkeypatch.setattr("myself_agent.orchestration_runtime.model_base_settings", unexpected)
    runner = DeepAgentRunner(store, run["id"], config_path=tmp_path / "no-official-key.env")
    assert runner.image == DEFAULT_IMAGE
