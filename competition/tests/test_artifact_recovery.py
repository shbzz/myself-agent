"""Real filesystem atomic rollback, no production data or model calls."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from test_orchestration_api import QueuedManager

from myself_agent.artifact_store import exchange, inventory
from myself_agent.general_settings import ConfigurationError
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.jobs import BusyError


def setup_artifacts(tmp_path):
    store = AgentStore(tmp_path)
    workspace = store.create_workspace("artifacts")
    session = store.create_session(workspace["id"], "rollback")
    directory = store.directory(workspace["id"])
    (directory / "script").write_text("original")
    (directory / "script").chmod(0o750)
    (directory / ".git").mkdir()
    (directory / ".git/config").write_text("synthetic git metadata")
    snapshot = store.artifacts.create(
        workspace["id"], {"request_id": uuid.uuid4().hex, "note": "before work"}
    )
    (directory / "script").write_text("edited")
    (directory / "extra").write_text("keep this displaced file")
    body = {
        "request_id": uuid.uuid4().hex,
        "snapshot_id": snapshot["id"],
        "expected_current_sha256": inventory(directory)["sha256"],
        "note": "explicit restoration",
        "confirm": "restore",
    }
    return store, workspace, session, directory, snapshot, body


def test_atomic_rollback_preserves_displaced_files_modes_git_and_database(tmp_path):
    store, workspace, session, directory, snapshot, body = setup_artifacts(tmp_path)
    result = store.artifacts.rollback(workspace["id"], body)
    assert result["state"] == "completed"
    assert (directory / "script").read_text() == "original"
    assert (directory / "script").stat().st_mode & 0o777 == 0o750
    assert (directory / ".git/config").read_text() == "synthetic git metadata"
    assert not (directory / "extra").exists()
    displaced = store.artifacts.directory / result["displaced_id"]
    assert (displaced / "script").read_text() == "edited"
    assert (displaced / "extra").read_text() == "keep this displaced file"
    assert inventory(directory) == snapshot["manifest"]
    assert store.session(session["id"])["workspace_id"] == workspace["id"]
    assert store.artifacts.rollback(workspace["id"], body) == result
    # Old content is an addressable snapshot; it was not merely left in an opaque temp folder.
    assert result["displaced_id"] in [
        item["id"] for item in store.artifacts.view(workspace["id"])["snapshots"]
    ]


def test_stale_confirmation_does_not_overwrite_user_edits(tmp_path):
    store, workspace, _, directory, _, body = setup_artifacts(tmp_path)
    (directory / "script").write_text("new user edit")
    with pytest.raises(ConfigurationError, match="fingerprint"):
        store.artifacts.rollback(workspace["id"], body)
    assert (directory / "script").read_text() == "new user edit"
    assert not store.artifacts.view(workspace["id"])["rollbacks"]


@pytest.mark.parametrize("after_exchange", [False, True])
def test_uncertain_exchange_is_fenced_and_reconciled_without_replay(
    tmp_path, monkeypatch, after_exchange
):
    store, workspace, session, directory, _, body = setup_artifacts(tmp_path)
    invocations = []

    def fail(left, right):
        invocations.append(True)
        with pytest.raises(BusyError, match="unresolved"):
            store.submit(session["id"], uuid.uuid4().hex, "must not run during exchange")
        with pytest.raises(BusyError, match="active"):
            store.artifacts.reconcile(workspace["id"], body["request_id"])
        if after_exchange:
            exchange(left, right)
        raise OSError("injected lost acknowledgement")

    monkeypatch.setattr("myself_agent.artifact_store.exchange", fail)
    with pytest.raises(OSError):
        store.artifacts.rollback(workspace["id"], body)
    with pytest.raises(BusyError, match="unresolved"):
        store.submit(session["id"], uuid.uuid4().hex, "must not run while uncertain")
    # Same request never retries the exchange.
    assert store.artifacts.rollback(workspace["id"], body)["state"] == "uncertain"
    reopened = AgentStore(tmp_path)
    result = reopened.artifacts.reconcile(workspace["id"], body["request_id"])
    assert result["state"] == ("completed" if after_exchange else "not_applied")
    assert len(invocations) == 1
    assert (directory / "script").read_text() == ("original" if after_exchange else "edited")
    run, created = reopened.submit(session["id"], uuid.uuid4().hex, "allowed after reconciliation")
    assert created and run["status"] == "queued"


def test_unknown_intervening_edit_remains_fenced_and_retained(tmp_path, monkeypatch):
    store, workspace, session, directory, _, body = setup_artifacts(tmp_path)

    def changed(left, right):
        exchange(left, right)
        (left / "script").write_text("post-exchange external edit")

    monkeypatch.setattr("myself_agent.artifact_store.exchange", changed)
    result = store.artifacts.rollback(workspace["id"], body)
    assert result["state"] == "uncertain"
    assert (directory / "script").read_text() == "post-exchange external edit"
    assert (store.artifacts.directory / result["displaced_id"] / "extra").is_file()
    with pytest.raises(BusyError):
        store.submit(session["id"], uuid.uuid4().hex, "do not assume recovery")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "oversize"])
def test_snapshot_rejects_unsafe_entries_and_cleans_only_new_copy(tmp_path, kind):
    store = AgentStore(tmp_path / "data")
    workspace = store.create_workspace("unsafe")
    directory = store.directory(workspace["id"])
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    if kind == "symlink":
        (directory / "entry").symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, directory / "entry")
    elif kind == "fifo":
        os.mkfifo(directory / "entry")
    else:
        with (directory / "entry").open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ConfigurationError):
        store.artifacts.create(workspace["id"], {"request_id": uuid.uuid4().hex, "note": "unsafe"})
    assert outside.read_text() == "preserve"
    assert not list(store.artifacts.directory.iterdir())


def test_cross_workspace_tamper_and_request_collision_refused(tmp_path):
    store, workspace, _, directory, snapshot, body = setup_artifacts(tmp_path)
    other = store.create_workspace("other")
    other_body = body | {
        "expected_current_sha256": inventory(store.directory(other["id"]))["sha256"]
    }
    with pytest.raises(KeyError):
        store.artifacts.rollback(other["id"], other_body)
    (store.artifacts.directory / snapshot["id"] / "script").write_text("tamper")
    with pytest.raises(ConfigurationError, match="integrity"):
        store.artifacts.rollback(workspace["id"], body)
    assert (directory / "script").read_text() == "edited"


def test_api_artifact_confirmation_same_origin_and_busy_fences(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=QueuedManager)) as client:
        workspace = client.post("/next/api/workspaces", json={"name": "api"}).json()
        base = f"/next/api/orchestration/workspaces/{workspace['id']}"
        body = {"request_id": uuid.uuid4().hex, "note": "explicit snapshot"}
        assert (
            client.post(
                base + "/snapshots", json=body, headers={"origin": "https://evil.example"}
            ).status_code
            == 403
        )
        saved = client.post(base + "/snapshots", json=body)
        assert saved.status_code == 201
        snapshot = saved.json()
        assert client.post(base + "/snapshots", json=body).json() == snapshot
        rollback = {
            "request_id": uuid.uuid4().hex,
            "snapshot_id": snapshot["id"],
            "expected_current_sha256": snapshot["manifest"]["sha256"],
            "note": "restore",
        }
        assert client.post(base + "/rollbacks", json=rollback).status_code == 422
        assert (
            client.post(base + "/rollbacks", json=rollback | {"confirm": "restore"}).json()["state"]
            == "completed"
        )
        session = client.post(
            "/next/api/sessions", json={"workspace_id": workspace["id"], "title": "busy"}
        ).json()
        store = client.app.state.manager.store
        store.submit(session["id"], uuid.uuid4().hex, "queued writer")
        assert (
            client.post(
                base + "/snapshots", json=body | {"request_id": uuid.uuid4().hex}
            ).status_code
            == 409
        )
