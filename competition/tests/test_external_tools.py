import io
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.tools import ToolException
from pydantic import SecretStr, ValidationError
from test_general import ScriptModel, call, new_run

from myself_agent.external_tools import external_tools
from myself_agent.general_runtime import run_agent
from myself_agent.general_settings import ConfigurationError, MCPServerInput, PolicyInput
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.mcp_tools import MCPAccess
from myself_agent.web_tools import MAX_BODY, PageText, WebAccess
from myself_agent.workspace_files import WorkspaceFiles


@pytest.fixture(scope="session")
def mcp_endpoint(tmp_path_factory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = tmp_path_factory.mktemp("mcp") / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("mcp_fixture.py")), str(port)],
            stdout=log,
            stderr=log,
        )
        endpoint = f"http://127.0.0.1:{port}/mcp"
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(log_path.read_text()[-3000:])
                try:
                    urllib.request.urlopen(endpoint, timeout=0.2)
                except urllib.error.HTTPError as error:
                    if error.code == 401:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Local MCP fixture did not start")
            yield endpoint
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def profile(store, endpoint, allowed=None):
    value = store.config.save_mcp_server(
        MCPServerInput(
            name="fixture",
            url=endpoint,
            api_key=SecretStr("fixture-credential"),
            allowed_tools=allowed or ["add", "echo", "fail", "pause", "oversized"],
        )
    )
    return store.config.get("mcp", value["id"])


def policy(**overrides):
    return {
        "web_enabled": True,
        "browser_enabled": True,
        "allowed_web_hosts": ["example.com"],
        "max_web_requests": 20,
        "tool_timeout_seconds": 20,
        "mcp_server_ids": [],
        "max_mcp_calls": 20,
    } | overrides


@pytest.mark.parametrize(
    "host",
    [
        "*",
        "*.example.com",
        "127.0.0.1",
        "169.254.169.254",
        "localhost",
        "service.local",
        "https://example.com",
        "example.com/path",
        "[::1]",
    ],
)
def test_policy_rejects_unsafe_domain_entries(host):
    with pytest.raises(ValidationError):
        PolicyInput(allowed_web_hosts=[host])


def test_policy_inheritance_snapshot_and_secret_redaction(tmp_path, mcp_endpoint):
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("tools")
    session = store.create_session(workspace["id"], "tools")
    server = profile(store, mcp_endpoint, ["add"])
    identifier = server["id"]
    with pytest.raises(ConfigurationError):
        store.config.save_policy("global", PolicyInput(web_enabled=True))
    store.config.save_policy(
        "global", PolicyInput(web_enabled=True, allowed_web_hosts=["EXAMPLE.COM."])
    )
    store.config.save_policy(
        "workspace:" + workspace["id"],
        PolicyInput(browser_enabled=True, mcp_server_ids=[identifier], max_mcp_calls=3),
    )
    run = new_run(store, session)
    before = store.run_configuration(run["id"])
    assert before["policy"]["allowed_web_hosts"] == ["example.com"]
    assert before["policy"]["browser_enabled"]
    assert before["policy"]["max_mcp_calls"] == 3
    assert before["mcp_servers"][0]["version"] == 1
    public = json.dumps(store.run(run["id"]))
    assert "fixture-credential" not in public and "credential_ref" not in public
    store.config.save_mcp_server(
        MCPServerInput(
            id=identifier,
            name="updated",
            url=mcp_endpoint,
            allowed_tools=["echo"],
            api_key=SecretStr("changed-fixture-credential"),
        )
    )
    assert store.run_configuration(run["id"])["mcp_servers"][0]["allowed_tools"] == ["add"]
    assert "add" in json.dumps(MCPAccess(store, before["mcp_servers"], None, 5).execute(identifier))
    store.config.save_policy(
        "session:" + session["id"],
        PolicyInput(web_enabled=False, browser_enabled=False, mcp_server_ids=[]),
    )
    assert not store.config.resolve(session_id=session["id"])["policy"]["web_enabled"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://public.example/mcp",
        "https://u:p@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp#x",
        "http://169.254.169.254/latest",
        "https://example.com:99999/mcp",
        "http://0.0.0.0/mcp",
        "http://[::]/mcp",
        "http://203.0.113.1/mcp",
        "https://example.com/mcp\n",
    ],
)
def test_mcp_endpoint_validation(url):
    with pytest.raises(ValidationError):
        MCPServerInput(name="invalid", url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://127.0.0.1",
        "file:///etc/passwd",
        "https://u:p@example.com",
        "https://example.com:8443",
        "https://sub.example.com",
        "https://evil.com",
    ],
)
def test_web_rejects_invalid_or_nonallowlisted_urls(url):
    with pytest.raises(ToolException):
        WebAccess(policy(), None).validate(url)


def test_dns_private_and_mixed_answers_are_denied_before_request():
    web = WebAccess(policy(), None)
    for addresses in [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["93.184.216.34", "10.0.0.1"],
        ["224.0.0.1"],
    ]:
        output = subprocess.CompletedProcess([], 0, stdout=json.dumps(addresses))
        with patch("myself_agent.web_tools.subprocess.run", return_value=output):
            with pytest.raises(ToolException, match="禁止"):
                web.resolve("example.com", time.monotonic() + 10)


def test_public_address_is_pinned_with_original_tls_name_and_host(monkeypatch):
    web = WebAccess(policy(), "http://127.0.0.1:7890")
    original_client = httpx.Client
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=httpx.ByteStream(
                b"<title>Reference</title><script>secret()</script><p>answer 42</p>"
            ),
        )

    monkeypatch.setattr(
        "myself_agent.web_tools.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), trust_env=False),
    )
    result = subprocess.CompletedProcess([], 0, stdout='["93.184.216.34"]')
    with patch("myself_agent.web_tools.subprocess.run", return_value=result) as dns:
        output = web.read("https://example.com/doc", time.monotonic() + 5)
        web.read("https://example.com/other", time.monotonic() + 5)
    assert dns.call_count == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "example.com"
    assert requests[0].extensions["sni_hostname"] == "example.com"
    assert output["title"] == "Reference" and "answer 42" in output["untrusted_content"]
    assert "secret" not in output["untrusted_content"]


def test_redirects_limits_and_response_bounds(monkeypatch):
    web = WebAccess(policy(max_web_requests=1), None)
    monkeypatch.setattr(web, "resolve", lambda host, deadline: "93.184.216.34")
    monkeypatch.setattr(
        web, "exchange", lambda *args: (302, {"location": "https://127.0.0.1/private"}, b"")
    )
    with pytest.raises(ToolException):
        web.fetch("https://example.com", time.monotonic() + 5)
    monkeypatch.setattr(web, "exchange", lambda *args: (200, {"content-type": "text/plain"}, b"ok"))
    with pytest.raises(ToolException, match="额度"):
        web.fetch("https://example.com", time.monotonic() + 5)
    original_client = httpx.Client
    for headers, body in [
        ({"content-encoding": "gzip"}, b"compressed"),
        ({}, b"x" * (MAX_BODY + 1)),
    ]:
        monkeypatch.setattr(
            "myself_agent.web_tools.httpx.Client",
            lambda headers=headers, body=body, **kwargs: original_client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
                ),
                trust_env=False,
            ),
        )
        with pytest.raises(ToolException):
            WebAccess.exchange(
                web, "https://example.com", "example.com", "93.184.216.34", time.monotonic() + 5
            )


def test_real_mcp_discovery_call_errors_and_budget(tmp_path, mcp_endpoint):
    store = AgentStore(tmp_path)
    server = profile(store, mcp_endpoint, ["add", "fail"])
    mcp = MCPAccess(store, [server], None, 5, max_calls=3)
    listing = mcp.execute(server["id"])
    assert [tool["name"] for tool in listing["tools"]] == ["add", "fail"]
    result = mcp.execute(server["id"], tool_name="add", arguments={"a": 19, "b": 23})
    assert "42" in result["untrusted_content"]
    with pytest.raises(ToolException, match="白名单"):
        mcp.execute(server["id"], tool_name="echo")
    with pytest.raises(ToolException, match="返回错误"):
        mcp.execute(server["id"], tool_name="fail")
    with pytest.raises(ToolException, match="额度"):
        mcp.execute(server["id"])
    with pytest.raises(ToolException, match="未在"):
        mcp.execute("missing")


def test_real_mcp_timeout_oversized_and_secret_redaction(tmp_path, mcp_endpoint):
    store = AgentStore(tmp_path)
    server = profile(store, mcp_endpoint)
    mcp = MCPAccess(store, [server], None, 1)
    started = time.monotonic()
    with pytest.raises(ToolException):
        mcp.execute(server["id"], tool_name="pause")
    assert time.monotonic() - started < 3
    with pytest.raises(ToolException):
        MCPAccess(store, [server], None, 5).execute(server["id"], tool_name="oversized")
    result = MCPAccess(store, [server], None, 5).execute(
        server["id"], tool_name="echo", arguments={"text": "fixture-credential"}
    )
    assert "fixture-credential" not in json.dumps(result)
    assert "credential redacted" in json.dumps(result)


def test_mcp_profile_api_and_connection_test_do_not_expose_secrets(tmp_path, mcp_endpoint):
    class Manager:
        def __init__(self, root, config):
            self.store = AgentStore(root)

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=Manager)) as client:
        response = client.post(
            "/next/api/mcp/servers",
            json={
                "name": "fixture",
                "url": mcp_endpoint,
                "api_key": "fixture-credential",
                "allowed_tools": ["add"],
            },
        )
        assert response.status_code == 201
        saved = response.json()
        assert saved["has_key"]
        response = client.post(f"/next/api/mcp/servers/{saved['id']}/test", json={})
        assert response.status_code == 200
        assert len(response.json()["tools"]) == 5
        response = client.post(
            "/next/api/settings/global/global", json={"mcp_server_ids": [saved["id"]]}
        )
        assert response.status_code == 200
        for url in ["/next/api/mcp/servers", "/next/api/configuration"]:
            value = client.get(url).text
            assert "fixture-credential" not in value and "credential_ref" not in value
        assert (
            client.post(
                "/next/api/mcp/servers",
                json={"name": "bad", "url": "file:///x", "api_key": "hidden-key"},
            ).status_code
            == 422
        )
        assert (
            "hidden-key"
            not in client.post(
                "/next/api/mcp/servers",
                json={"name": "bad", "url": "file:///x", "api_key": "hidden-key"},
            ).text
        )


@pytest.mark.parametrize("scope", ["global", "workspace", "session"])
def test_clearing_active_mcp_allowlist_keeps_settings_editable(tmp_path, scope):
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("recovery")
    session = store.create_session(workspace["id"], "recovery")
    server = profile(store, "https://example.com/mcp", ["add"])
    identifier = server["id"]
    scope_id = {"global": "global", "workspace": workspace["id"], "session": session["id"]}[scope]
    settings_url = f"/next/api/settings/{scope}/{scope_id}"

    class Manager:
        def __init__(self, root, config):
            self.store = store

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=Manager)) as client:
        assert client.post(settings_url, json={"mcp_server_ids": [identifier]}).status_code == 200
        run = new_run(store, session)
        saved = client.post(
            "/next/api/mcp/servers",
            json={"id": identifier, "name": "deny all", "url": server["url"], "allowed_tools": []},
        )
        assert saved.status_code == 201
        config = client.get("/next/api/configuration", params={"session_id": session["id"]})
        assert config.status_code == 200
        current = config.json()
        assert current["policy"]["mcp_server_ids"] == [identifier]
        assert current["mcp_servers"][0]["allowed_tools"] == []
        assert "credential_ref" not in config.text and "fixture-credential" not in config.text
        access = MCPAccess(store, current["mcp_servers"], None, 5)
        with patch.object(access, "request") as request:
            with pytest.raises(ToolException, match="白名单"):
                access.execute(identifier, tool_name="add", arguments={"a": 1, "b": 2})
            request.assert_not_called()
        assert store.run_configuration(run["id"])["mcp_servers"][0]["allowed_tools"] == ["add"]
        assert client.post(settings_url, json={"max_mcp_calls": 4}).status_code == 200
        assert client.post(settings_url, json={"mcp_server_ids": []}).status_code == 200
        recovered = client.get("/next/api/configuration", params={"session_id": session["id"]})
        assert recovered.status_code == 200
        assert recovered.json()["mcp_servers"] == []
        assert recovered.json()["policy"]["max_mcp_calls"] == 4


@pytest.mark.docker
def test_imported_project_uses_web_browser_mcp_then_real_execution(
    tmp_path, settings, mcp_endpoint, monkeypatch
):
    store = AgentStore(tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("README.md", "Existing project")
    workspace = WorkspaceFiles(store).import_archive("联网工具闭环", archive.getvalue())[
        "workspace"
    ]
    session = store.create_session(workspace["id"], "工具验证")
    server = profile(store, mcp_endpoint, ["add"])
    store.config.save_policy(
        "workspace:" + workspace["id"],
        PolicyInput(
            web_enabled=True,
            browser_enabled=True,
            allowed_web_hosts=["example.com"],
            mcp_server_ids=[server["id"]],
        ),
    )
    exchanges = []

    def response(self, url, host, address, deadline):
        exchanges.append(url)
        return (
            200,
            {"content-type": "text/html"},
            (
                b"<html><head><title>Public reference</title></head><body><h1>Reference 42</h1>"
                b"<script>document.body.innerText='bad';fetch('http://127.0.0.1/private')</script>"
                b"<img src='http://169.254.169.254/latest'/></body></html>"
            ),
        )

    monkeypatch.setattr(WebAccess, "resolve", lambda *args: "93.184.216.34")
    monkeypatch.setattr(WebAccess, "exchange", response)
    model = ScriptModel(
        responses=[
            call("web_read", {"url": "https://example.com/reference"}),
            call("browser_read", {"url": "https://example.com/reference"}),
            call("mcp_list_tools", {"server_id": server["id"]}),
            call(
                "mcp_call",
                {"server_id": server["id"], "tool_name": "add", "arguments": {"a": 19, "b": 23}},
            ),
            call("write_file", {"file_path": "/workspace/result.txt", "content": "verified 42\n"}),
            call(
                "execute",
                {
                    "command": (
                        "python -c 'from pathlib import Path; "
                        'assert Path("result.txt").read_text().strip() == "verified 42"\''
                    )
                },
            ),
            AIMessage(content="资料读取、浏览器截图、MCP 调用及容器验证已完成。"),
        ]
    )
    run = new_run(store, session)
    run_agent(store, run["id"], model, settings.image)
    events = store.events(session["id"])
    capture = next(event for event in events if event["kind"] == "browser.captured")
    assert capture["payload"]["title"] == "Public reference"
    assert (
        store.attachment_path(capture["payload"]["attachment_id"])
        .read_bytes()
        .startswith(b"\x89PNG")
    )
    outputs = [event["payload"]["output"] for event in events if event["kind"] == "tool.finished"]
    assert any("Reference 42" in output for output in outputs)
    assert any("42" in output and "server_id" in output for output in outputs)
    assert not any(event["kind"] == "tool.failed" for event in events)
    assert all(url.startswith("https://example.com") for url in exchanges)
    assert (store.directory(workspace["id"]) / "result.txt").read_text().strip() == "verified 42"


def test_external_tools_are_not_registered_without_explicit_enablement(tmp_path):
    store = AgentStore(tmp_path)
    assert external_tools(store, uuid.uuid4().hex, store.config.resolve()) == []


def test_web_text_marks_truncation_and_ignores_malformed_links():
    parser = PageText("https://example.com")
    parser.feed('<a href="https://[invalid">bad link</a><p>' + "x" * 13000 + "</p>")
    value = parser.result()
    assert value["truncated"] and len(value["untrusted_content"]) == 12000
    assert value["links"] == []


def test_mcp_large_text_is_marked_truncated(tmp_path, mcp_endpoint):
    store = AgentStore(tmp_path)
    server = profile(store, mcp_endpoint, ["echo"])
    value = MCPAccess(store, [server], None, 5).execute(
        server["id"], tool_name="echo", arguments={"text": "x" * 13000}
    )
    assert value["truncated"] and len(value["untrusted_content"]) == 12000
