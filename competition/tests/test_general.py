import subprocess
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from myself_agent.general_manager import AgentManager
from myself_agent.general_runtime import run_agent
from myself_agent.general_sandbox import WorkspaceSandbox
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError


class ScriptModel(BaseChatModel):
    responses: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)
    names: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-general-tools"

    def bind_tools(self, tools, **kwargs):
        self.names.extend(t.name if hasattr(t, "name") else t["name"] for t in tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def call(name, args):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": uuid.uuid4().hex, "type": "tool_call"}],
    )


def new_run(store, session, text="任意任务"):
    return store.submit(session["id"], uuid.uuid4().hex, text)[0]


def test_persistence_idempotency_isolation_and_event_cursor(tmp_path):
    store = AgentStore(tmp_path)
    a, b = store.create_workspace("A"), store.create_workspace("B")
    session = store.create_session(a["id"], "任意任务")
    request = uuid.uuid4().hex
    run, created = store.submit(session["id"], request, "创建任意文件")
    assert created
    assert not store.submit(session["id"], request, "创建任意文件")[1]
    with pytest.raises(BusyError):
        store.submit(session["id"], request, "不同内容")
    store.event(run["id"], "tool.started", {"name": "execute"})
    first = store.events(session["id"])[0]
    store.event(run["id"], "tool.finished", {"output": "done"})
    assert len(store.events(session["id"], first["sequence"])) == 1
    assert store.sessions(b["id"]) == []
    assert store.directory(a["id"]) != store.directory(b["id"])
    reopened = AgentStore(tmp_path)
    assert reopened.run(run["id"])["text"] == "创建任意文件"


@pytest.mark.docker
def test_real_general_tools_failure_repair_and_persistent_followup(tmp_path, settings):
    store = AgentStore(tmp_path / "data")
    workspace = store.create_workspace("不限任务文件")
    session = store.create_session(workspace["id"], "温度换算")
    model = ScriptModel(
        responses=[
            call(
                "write_file",
                {
                    "file_path": "/workspace/temperature.py",
                    "content": "def fahrenheit(c):\n    return c + 32\n",
                },
            ),
            call(
                "execute",
                {
                    "command": "python -c 'from temperature import fahrenheit; "
                    "assert fahrenheit(100) == 212'"
                },
            ),
            call(
                "edit_file",
                {
                    "file_path": "/workspace/temperature.py",
                    "old_string": "return c + 32",
                    "new_string": "return c * 9 / 5 + 32",
                },
            ),
            call(
                "execute",
                {
                    "command": "python -c 'from temperature import fahrenheit; "
                    'assert fahrenheit(100) == 212; print("verified")\''
                },
            ),
            AIMessage(content="已修复换算函数，并通过实际测试。"),
        ]
    )
    run = new_run(store, session, "实现温度转换并测试")
    run_agent(store, run["id"], model, settings.image)
    store.status(run["id"], "succeeded")
    events = store.events(session["id"])
    outputs = [e["payload"].get("output", "") for e in events if e["kind"] == "tool.finished"]
    assert any("AssertionError" in output for output in outputs)
    assert any("verified" in output for output in outputs)
    assert any(e["kind"] == "file.changed" for e in events)
    assert "task" not in model.names
    assert "execute" in model.names and "read_file" in model.names
    followup = new_run(store, session, "继续检查上次实现")
    next_model = ScriptModel(
        responses=[
            call("read_file", {"file_path": "/workspace/temperature.py"}),
            AIMessage(content="保留了上次实现。"),
        ]
    )
    run_agent(store, followup["id"], next_model, settings.image)
    assert any(getattr(m, "content", "") == "实现温度转换并测试" for m in next_model.seen[0])
    assert "c * 9 / 5" in (store.directory(workspace["id"]) / "temperature.py").read_text()
    other = store.create_workspace("隔离")
    assert not (store.directory(other["id"]) / "temperature.py").exists()


@pytest.mark.docker
def test_container_boundary_and_timeout_cleanup(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "host-secret"
    secret.write_text("must stay on host")
    with WorkspaceSandbox(workspace, settings.image, uuid.uuid4().hex) as backend:
        result = backend.execute(
            f"test ! -e {secret} && test ! -e /var/run/docker.sock && "
            'test -z "$DEEPSEEK_API_KEY" && echo isolated'
        )
        assert result.exit_code == 0 and "isolated" in result.output
        with pytest.raises(RuntimeError, match="timed out"):
            backend.execute("sleep 20", timeout=1)
        result = subprocess.run(
            ["sudo", "-n", "docker", "inspect", backend.id], capture_output=True
        )
        assert result.returncode != 0


def test_supervisor_stop_and_restart_marks_interruption(tmp_path, monkeypatch):
    original = subprocess.Popen
    monkeypatch.setattr(
        "myself_agent.general_manager.subprocess.Popen",
        lambda *args, **kwargs: original(
            ["python3", "-c", "import time; time.sleep(60)"], start_new_session=True
        ),
    )
    monkeypatch.setattr(AgentManager, "cleanup", staticmethod(lambda run_id: None))
    manager = AgentManager(tmp_path, tmp_path / "unused.env")
    workspace = manager.store.create_workspace("stop")
    session = manager.store.create_session(workspace["id"], "stop")
    run = manager.submit(session["id"], uuid.uuid4().hex, "long task")
    for _ in range(100):
        if manager.store.run(run["id"])["status"] == "running":
            break
        time.sleep(0.01)
    assert manager.cancel(run["id"])["status"] == "cancelled"
    manager.close()
    interrupted = new_run(manager.store, session)
    restarted = AgentManager(tmp_path, tmp_path / "unused.env")
    assert restarted.store.run(interrupted["id"])["status"] == "interrupted"
    restarted.close()


def test_preview_api_boundaries(tmp_path):
    class NoExecution:
        def __init__(self, root, config):
            self.store = AgentStore(root)

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=NoExecution)) as client:
        assert client.get("/next/health").status_code == 200
        assert (
            client.post(
                "/next/api/workspaces",
                json={"name": "a"},
                headers={"origin": "https://unrelated.invalid"},
            ).status_code
            == 403
        )
        response = client.post("/next/api/workspaces", json={"name": "a"})
        assert response.status_code == 201
        workspace = response.json()
        session = client.post("/next/api/sessions", json={"workspace_id": workspace["id"]}).json()
        assert client.get("/next/api/sessions/" + session["id"]).json()["messages"] == []
        assert client.get("/next/api/sessions/missing").status_code == 404
        assert client.post("/next/api/workspaces", content="{}").status_code == 415
        assert client.post("/next/api/workspaces", json={"name": "a" * 70000}).status_code == 413
