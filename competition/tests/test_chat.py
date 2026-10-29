import json
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import replace

import pytest
from conftest import PARSER, STATS
from fastapi.testclient import TestClient

from myself_agent.jobs import JobManager
from myself_agent.schema import Plan, SourceFile
from myself_agent.tracking import changes, file_versions
from myself_agent.web import create_app
from myself_agent.workflow import Workflow, write_json


def wait_finished(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = client.get(f"/api/runs/{run_id}").json()
        if row["job"]["status"] not in ("queued", "running"):
            return row
        time.sleep(0.02)
    raise AssertionError("Test worker did not finish")


def test_conversation_followup_persists_baseline_and_live_file_changes(settings, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def runner(directory, request):
        calls.append(request)
        previous = (
            json.loads((directory / "baseline.json").read_text())
            if request["parent_run_id"]
            else {}
        )
        if previous:
            assert previous["stats.py"] == STATS
        content = STATS + ("\n# Updated in second message\n" if previous else "")
        write_json(
            directory / "stats_developer-round-0.json", {"filename": "stats.py", "content": content}
        )
        if not previous:
            entered.set()
            assert release.wait(5)
        summary = {"outcome": "passed", "run_id": directory.name}
        write_json(directory / "summary.json", summary)
        return summary

    config = replace(settings, run_root=tmp_path / "runs")
    app = create_app(config, lambda conf, path: JobManager(conf, path, runner=runner))
    conversation_id = uuid.uuid4().hex
    initial = {
        "request_id": uuid.uuid4().hex,
        "scenario": "csv-orders",
        "brief": "First requirement",
        "concurrency": 2,
        "max_repairs": 1,
        "conversation_id": conversation_id,
    }
    with TestClient(app) as client:
        try:
            first = client.post("/api/runs", json=initial).json()
            assert entered.wait(2)
            live = client.get(f"/api/runs/{first['run_id']}").json()
            assert live["job"]["status"] == "running"
            assert live["artifacts"]["stats.py"] == STATS  # No verification workspace exists yet.
            assert live["changes"][0]["versions"][0]["added"] > 0
        finally:
            release.set()
        wait_finished(client, first["run_id"])
        body = {
            **initial,
            "request_id": uuid.uuid4().hex,
            "brief": "Add a comment",
            "parent_run_id": first["run_id"],
        }
        response = client.post("/api/runs", json=body)
        assert response.status_code == 202
        second = wait_finished(client, response.json()["run_id"])
        assert second["job"]["parent_run_id"] == first["run_id"]
        assert second["changes"][0]["based_on_previous_task"] is True
        assert second["changes"][0]["versions"][0]["removed"] == 0
        assert second["changes"][0]["versions"][0]["added"] == 2
        assert client.post("/api/runs", json=body).json()["run_id"] == second["job"]["run_id"]
        assert len(calls) == 2
        sessions = client.get("/api/conversations").json()["conversations"]
        assert len(sessions) == 1 and sessions[0]["turns"] == 2
        turns = client.get(f"/api/conversations/{conversation_id}").json()["turns"]
        assert [turn["job"]["brief"] for turn in turns] == ["First requirement", "Add a comment"]
        stale = {**body, "request_id": uuid.uuid4().hex}
        assert client.post("/api/runs", json=stale).status_code == 409
        wrong_session = {**stale, "conversation_id": uuid.uuid4().hex}
        assert client.post("/api/runs", json=wrong_session).status_code == 409


def test_repair_versions_show_real_line_changes_and_ignore_unapproved_response(tmp_path):
    from myself_agent.scenarios import get_scenario

    write_json(
        tmp_path / "stats_developer-round-0.json",
        {"filename": "stats.py", "content": "def summarize(orders):\n    return {}\n"},
    )
    write_json(
        tmp_path / "stats_developer-round-1.json", {"filename": "stats.py", "content": STATS}
    )
    write_json(
        tmp_path / "parser_developer-round-0.json",
        {"filename": "secrets.env", "content": "not an approved file"},
    )
    versions = file_versions(tmp_path, get_scenario("csv-orders"))
    assert set(versions) == {"stats.py"}
    tracked = changes(versions, {})[0]
    assert len(tracked["versions"]) == 2
    fixed = tracked["versions"][-1]
    assert fixed["round"] == 1 and fixed["removed"] == 1 and fixed["added"] == 1
    assert "-    return {}" in fixed["diff"]


def test_failed_followup_preserves_existing_code_for_the_next_message(settings, tmp_path):
    config = replace(settings, run_root=tmp_path / "runs")
    received = []

    def runner(directory, request):
        received.append(json.loads((directory / "baseline.json").read_text()))
        summary = {"outcome": "passed", "run_id": directory.name}
        write_json(directory / "summary.json", summary)
        return summary

    app = create_app(config, lambda conf, path: JobManager(conf, path, runner=runner))
    with TestClient(app) as client:
        manager = app.state.jobs
        parent_id, conversation_id = uuid.uuid4().hex, uuid.uuid4().hex
        parent_directory = config.run_root / parent_id
        parent_directory.mkdir()
        write_json(parent_directory / "baseline.json", {"parser.py": PARSER, "stats.py": STATS})
        with manager.connect() as db:
            db.execute(
                "INSERT INTO jobs (run_id,request_id,scenario,brief,concurrency,max_repairs,"
                "status,created,updated,error_type,conversation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    parent_id,
                    uuid.uuid4().hex,
                    "csv-orders",
                    "Failed before model output",
                    2,
                    1,
                    "error",
                    "2026-10-29T08:00:00+00:00",
                    "2026-10-29T08:00:00+00:00",
                    "RuntimeError",
                    conversation_id,
                ),
            )
        assert client.get(f"/api/runs/{parent_id}").json()["artifacts"]["stats.py"] == STATS
        response = client.post(
            "/api/runs",
            json={
                "request_id": uuid.uuid4().hex,
                "scenario": "csv-orders",
                "conversation_id": conversation_id,
                "parent_run_id": parent_id,
                "brief": "Continue the existing work",
            },
        )
        assert response.status_code == 202
        assert wait_finished(client, response.json()["run_id"])["job"]["status"] == "passed"
        assert received == [{"parser.py": PARSER, "stats.py": STATS}]


@pytest.mark.docker
def test_followup_development_receives_previous_source(monkeypatch, settings, tmp_path):
    prompts = {}
    baseline = {
        "parser.py": PARSER + "\n# Previous parser marker\n",
        "stats.py": STATS + "\n# Previous statistics marker\n",
    }

    def fake_role(model, role, schema, prompt):
        if schema is Plan:
            return Plan(
                tasks=[
                    {
                        "filename": name,
                        "instruction": "Preserve and update the module",
                        "depends_on": [],
                    }
                    for name in baseline
                ]
            ), {}
        filename = role.replace("_developer", ".py")
        prompts[filename] = prompt
        return SourceFile(
            filename=filename, content=baseline[filename] + "\n# Current update\n"
        ), {}

    monkeypatch.setattr("myself_agent.workflow.invoke_role", fake_role)
    monkeypatch.setattr("myself_agent.workflow.model_session", lambda _: nullcontext(None))
    run = tmp_path / "run"
    run.mkdir()
    result = Workflow(
        settings, run, uuid.uuid4().hex, brief="Add a comment", base_artifacts=baseline
    ).run()
    assert result["outcome"] == "passed" and result["based_on_previous_task"] is True
    for name, source in baseline.items():
        assert source in prompts[name]
