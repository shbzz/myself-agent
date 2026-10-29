import base64
import struct
import uuid
import zlib

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from test_general import ScriptModel, call

from myself_agent.general_model import general_model_session
from myself_agent.general_runtime import run_agent
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError


def png_image():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return png + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


@pytest.mark.docker
def test_image_blocks_reasoning_and_tool_timing(tmp_path, settings):
    store = AgentStore(tmp_path / "images")
    workspace = store.create_workspace("images")
    session = store.create_session(workspace["id"], "vision")
    image = store.create_attachment(session["id"], "red.png", png_image())
    other = store.create_session(workspace["id"], "other")
    with pytest.raises(BusyError):
        store.submit(other["id"], uuid.uuid4().hex, "read", [image["id"]])
    with pytest.raises(ValueError):
        store.create_attachment(session["id"], "fake.png", b"<svg>not an image</svg>")
    run, _ = store.submit(session["id"], uuid.uuid4().hex, "read image", [image["id"]], "low")
    model = ScriptModel(
        responses=[
            call("execute", {"command": "printf inspected"}),
            AIMessage(
                content="Image processed",
                additional_kwargs={"reasoning_content": "fixture provider reasoning"},
            ),
        ]
    )
    run_agent(store, run["id"], model, settings.image)
    user = next(m for m in model.seen[0] if m.type == "human")
    assert user.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    events = store.events(session["id"])
    assert any(e["kind"] == "image.submitted" for e in events)
    ended = next(e for e in events if e["kind"] == "tool.finished")
    assert ended["payload"]["elapsed_ms"] >= 0
    started = next(e for e in events if e["kind"] == "tool.started")
    assert started["payload"]["inputs"]["command"] == "printf inspected"
    answer = store.messages(session["id"])[-1]["message"]
    assert answer["data"]["additional_kwargs"]["reasoning_content"] == "fixture provider reasoning"
    with general_model_session(settings, "low") as adapter:
        payload = adapter._get_request_payload(
            [
                HumanMessage(content=user.content),
                AIMessage(
                    content="answer", additional_kwargs={"reasoning_content": "provider reasoning"}
                ),
            ]
        )
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png")
        assert payload["messages"][1]["reasoning_content"] == "provider reasoning"
        assert payload["extra_body"]["thinking"]["type"] == "enabled"
        assert payload["extra_body"]["reasoning_effort"] == "low"


def test_image_api_persistence_and_invalid_upload(tmp_path):
    class NoExecution:
        def __init__(self, root, config):
            self.store = AgentStore(root)

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=NoExecution)) as client:
        workspace = client.post("/next/api/workspaces", json={"name": "pictures"}).json()
        session = client.post("/next/api/sessions", json={"workspace_id": workspace["id"]}).json()
        response = client.post(
            "/next/api/sessions/" + session["id"] + "/attachments",
            json={"filename": "test.png", "data_base64": base64.b64encode(png_image()).decode()},
        )
        assert response.status_code == 201
        image = client.get("/next/api/attachments/" + response.json()["id"])
        assert image.content == png_image()
        assert image.headers["content-type"] == "image/png"
        invalid = client.post(
            "/next/api/sessions/" + session["id"] + "/attachments",
            json={"filename": "invalid.png", "data_base64": "!!not-base64"},
        )
        assert invalid.status_code == 422
