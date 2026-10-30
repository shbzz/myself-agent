import json
import stat
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from deepagents import create_deep_agent
from fastapi.testclient import TestClient
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from test_general import ScriptModel
from test_general_vision import png_image

from myself_agent.general_context import ContextMiddleware, estimate_messages
from myself_agent.general_model import general_model_session, provider_credential
from myself_agent.general_runtime import run_agent
from myself_agent.general_settings import ConfigurationError, ModelInput, PolicyInput, ProviderInput
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app


def add_model(store, endpoint="http://127.0.0.1:9001/v1", key="fixture-private-value"):
    provider = store.config.save_provider(
        ProviderInput(name="Custom", protocol="openai_compatible", base_url=endpoint, api_key=key)
    )
    model = store.config.save_model(
        ModelInput(
            name="Custom model",
            provider_id=provider["id"],
            model_id="arbitrary-model-id",
            context_window_tokens=32768,
            max_output_tokens=4096,
            images=False,
            tools=False,
            streaming=False,
        )
    )
    return provider, model


def test_versioned_credentials_inheritance_and_frozen_run(tmp_path):
    store = AgentStore(tmp_path)
    provider, model = add_model(store)
    workspace = store.create_workspace("settings")
    session = store.create_session(workspace["id"], "snapshots")
    assert provider["has_key"] and "api_key" not in json.dumps(provider)
    private_provider = store.config.get("provider", provider["id"])
    path = tmp_path / "credentials" / private_provider["credential_ref"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "fixture-private-value" not in (tmp_path / "general.sqlite").read_bytes().decode(
        errors="ignore"
    )
    with pytest.raises(ConfigurationError):
        store.config.save_policy(
            "session:" + session["id"], PolicyInput(model_profile_id=model["id"])
        )
    store.config.save_policy("global", PolicyInput(max_model_calls=12))
    store.config.save_policy("workspace:" + workspace["id"], PolicyInput(max_model_calls=6))
    store.config.save_policy(
        "session:" + session["id"],
        PolicyInput(
            model_profile_id=model["id"],
            effective_context_tokens=32768,
            output_reserve_tokens=2048,
            max_model_calls=3,
        ),
    )
    run, _ = store.submit(session["id"], uuid.uuid4().hex, "hello", reasoning_effort="off")
    snapshot = store.run_configuration(run["id"])
    assert snapshot["policy"]["max_model_calls"] == 3
    assert snapshot["profile"]["model_id"] == "arbitrary-model-id"
    store.config.save_provider(
        ProviderInput(
            id=provider["id"],
            name="Changed",
            protocol="openai_compatible",
            base_url="http://127.0.0.1:9002/v1",
            api_key="updated-private-value",
        )
    )
    assert store.config.get("provider", provider["id"])["version"] == 2
    assert store.run_configuration(run["id"])["provider"]["base_url"].endswith("9001/v1")
    assert (
        store.run_configuration(run["id"])["provider"]["credential_ref"]
        == private_provider["credential_ref"]
    )
    assert "credential_ref" not in run["configuration"]["provider"]
    assert path.read_text() == "fixture-private-value"


def test_custom_provider_actual_http_request(tmp_path, settings):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(
                {
                    "path": self.path,
                    "body": data,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            body = json.dumps(
                {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": data["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "custom endpoint used"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = AgentStore(tmp_path)
        provider, model = add_model(store, f"http://127.0.0.1:{server.server_port}/v1")
        snapshot = store.config.resolve(
            override={
                "model_profile_id": model["id"],
                "effective_context_tokens": 32768,
                "output_reserve_tokens": 2048,
            }
        )
        credential = provider_credential(store, snapshot["provider"], settings)
        with general_model_session(settings, "off", snapshot, credential) as adapter:
            assert adapter.invoke("test").content == "custom endpoint used"
        assert captured[0]["path"] == "/v1/chat/completions"
        assert captured[0]["body"]["model"] == "arbitrary-model-id"
        assert captured[0]["authorization"] == "Bearer fixture-private-value"
        assert (
            captured[0]["body"].get("max_completion_tokens", captured[0]["body"].get("max_tokens"))
            == 2048
        )
    finally:
        server.shutdown()
        server.server_close()


def test_compaction_pairs_images_and_archive(tmp_path):
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("context")
    session = store.create_session(workspace["id"], "context")
    image = store.create_attachment(session["id"], "image.png", png_image())
    run, _ = store.submit(session["id"], uuid.uuid4().hex, "compact")
    snapshot = store.run_configuration(run["id"])
    snapshot["policy"]["keep_recent_messages"] = 2
    messages = [
        HumanMessage(
            content="Remember marker 37", additional_kwargs={"attachment_ids": [image["id"]]}
        ),
        AIMessage(
            content="",
            tool_calls=[{"id": "call1", "name": "execute", "args": {"command": "echo 37"}}],
        ),
        ToolMessage(content="37", tool_call_id="call1"),
        AIMessage(content="verified 37"),
        HumanMessage(content="continue"),
        AIMessage(content="continuing"),
    ]
    model = ScriptModel(
        responses=[AIMessage(content="用户目标：保留37，已验证echo 37，图片引用仍可读取。")]
    )
    middleware = ContextMiddleware(
        model, snapshot, store, run["id"], UsageMetadataCallbackHandler(), force=True
    )
    result = middleware.before_model({"messages": messages}, None)
    retained = [m for m in result["messages"] if m.type != "remove"]
    assert retained[0].additional_kwargs["attachment_ids"] == [image["id"]]
    assert retained[0].content[1]["type"] == "image_url"
    assert retained[-2:] == messages[-2:]
    assert any("37" in block.get("text", "") for block in retained[0].content)
    assert store.config.records(session["id"])[0]["value"]["attachment_ids"] == [image["id"]]
    assert len(messages) == 6  # Source history is not mutated/deleted.


@pytest.mark.docker
def test_runtime_automatically_compacts_long_checkpoint(tmp_path, settings):
    store = AgentStore(tmp_path / "runtime")
    workspace = store.create_workspace("context")
    session = store.create_session(workspace["id"], "context")
    store.config.save_policy(
        "session:" + session["id"],
        PolicyInput(
            effective_context_tokens=32768,
            output_reserve_tokens=1024,
            compression_trigger_ratio=0.5,
            compression_target_ratio=0.2,
            keep_recent_messages=2,
        ),
    )
    run, _ = store.submit(session["id"], uuid.uuid4().hex, "继续，关键编号是什么")
    model = ScriptModel(
        responses=[
            AIMessage(content="目标编号37。之前已经完成读取。"),
            AIMessage(content="关键编号是37。"),
        ]
    )
    config = {"configurable": {"thread_id": session["id"]}}
    with SqliteSaver.from_conn_string(str(store.root / "checkpoints.sqlite")) as saver:
        seed = create_deep_agent(model=model, checkpointer=saver)
        seed.update_state(
            config,
            {
                "messages": [
                    HumanMessage(content="编号37 " + "x" * 50000),
                    AIMessage(content="已记住37"),
                    HumanMessage(content="后续要求"),
                    AIMessage(content="收到"),
                ]
            },
            as_node="model",
        )
    run_agent(store, run["id"], model, settings.image)
    records = store.config.records(session["id"])
    assert (
        records
        and records[0]["value"]["estimated_after_tokens"]
        < records[0]["value"]["estimated_before_tokens"]
    )
    assert estimate_messages(model.seen[-1]) < 15000
    assert not any(isinstance(m.content, str) and "x" * 10000 in m.content for m in model.seen[-1])
    assert any(e["kind"] == "context.compressed" for e in store.events(session["id"]))


def test_settings_api_does_not_echo_keys_or_accept_invalid_capacity(tmp_path):
    class NoExecution:
        def __init__(self, root, config):
            self.store = AgentStore(root)

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=NoExecution)) as client:
        response = client.post(
            "/next/api/providers",
            json={
                "name": "fixture",
                "protocol": "openai_compatible",
                "base_url": "http://127.0.0.1:9000/v1",
                "api_key": "fixture-private-value",
            },
        )
        assert response.status_code == 201
        assert "fixture-private-value" not in response.text
        assert "fixture-private-value" not in client.get("/next/api/providers").text
        invalid = client.post(
            "/next/api/providers",
            json={
                "name": "fixture",
                "protocol": "unsupported",
                "base_url": "http://127.0.0.1:9000",
                "api_key": "fixture-private-value",
            },
        )
        assert invalid.status_code == 422 and "fixture-private-value" not in invalid.text
        assert (
            client.post(
                "/next/api/settings/global/global", json={"effective_context_tokens": 2097152}
            ).status_code
            == 422
        )


def test_model_call_budget_includes_compaction_and_capabilities(tmp_path):
    from myself_agent.general_context import CallBudget

    budget = CallBudget(1)
    model = ScriptModel(responses=[AIMessage(content="first"), AIMessage(content="must not run")])
    model.invoke("one", config={"callbacks": [budget]})
    with pytest.raises(ConfigurationError, match="调用预算"):
        model.invoke("two", config={"callbacks": [budget]})
    store = AgentStore(tmp_path)
    provider, profile = add_model(store)
    workspace = store.create_workspace("images")
    session = store.create_session(workspace["id"], "images")
    image = store.create_attachment(session["id"], "image.png", png_image())
    store.config.save_policy(
        "session:" + session["id"],
        PolicyInput(
            model_profile_id=profile["id"],
            effective_context_tokens=32768,
            output_reserve_tokens=2048,
        ),
    )
    with pytest.raises(ConfigurationError, match="包含图片"):
        store.submit(session["id"], uuid.uuid4().hex, "read", [image["id"]], "off")
    with pytest.raises(ConfigurationError, match="思考等级"):
        store.submit(session["id"], uuid.uuid4().hex, "text", reasoning_effort="high")
