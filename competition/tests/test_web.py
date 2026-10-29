import io
import json
import threading
import time
import uuid
import zipfile
from dataclasses import replace

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from myself_agent.jobs import JobManager
from myself_agent.web import create_app
from myself_agent.workflow import write_json


def payload():
    return {
        "request_id": uuid.uuid4().hex,
        "scenario": "csv-orders",
        "brief": "中文注释",
        "concurrency": 2,
        "max_repairs": 1,
    }


def test_web_job_idempotency_quota_and_evidence(settings, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def runner(directory, request):
        entered.set()
        assert release.wait(5)
        workspace = directory / "round-0" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "stats.py").write_text("def summarize(orders): return {}")
        summary = {"outcome": "passed", "run_id": directory.name}
        write_json(directory / "summary.json", summary)
        return summary

    config = replace(settings, run_root=tmp_path / "runs")
    app = create_app(config, lambda conf, path: JobManager(conf, path, runner=runner))
    with TestClient(app) as client:
        try:
            body = payload()
            response = client.post("/api/runs", json=body)
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            assert entered.wait(2)
            assert client.get("/health").json() == {"status": "ok"}
            assert client.post("/api/runs", json=body).json()["run_id"] == run_id
            assert client.post("/api/runs", json=payload()).status_code == 409
            assert client.post("/api/runs", json={**body, "brief": "different"}).status_code == 409
            assert client.get(f"/api/runs/{run_id}/download").status_code == 409
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = client.get(f"/api/runs/{run_id}").json()
            if detail["job"]["status"] == "passed":
                break
            time.sleep(0.02)
        assert detail["job"]["status"] == "passed"
        assert "stats.py" in detail["artifacts"]
        content = client.get(f"/api/runs/{run_id}/download")
        archive = zipfile.ZipFile(io.BytesIO(content.content))
        assert "round-0/workspace/stats.py" in archive.namelist()
        assert "summary.json" in archive.namelist()
        assert all("env" not in name and "process.log" not in name for name in archive.namelist())
        assert "test-placeholder" not in json.dumps(detail)


def test_browser_origin_host_and_input_boundaries(settings, tmp_path):
    config = replace(settings, run_root=tmp_path / "runs")
    with TestClient(create_app(config)) as client:
        page = client.get("/")
        assert page.status_code == 200 and "编排" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        assert len(client.get("/api/meta").json()["scenarios"]) == 3
        assert (
            client.post(
                "/api/runs", json=payload(), headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert client.post("/api/runs", content="{}").status_code == 415
        assert (
            client.post("/api/runs", json={**payload(), "scenario": "../../etc/passwd"}).status_code
            == 422
        )
        assert client.post("/api/runs", json={**payload(), "brief": "x" * 1501}).status_code == 422
        assert client.get("/api/runs/invalid").status_code == 404
        assert client.get("/health", headers={"Host": "evil.invalid"}).status_code == 400


def test_restart_marks_unfinished_job_without_automatic_new_api_calls(settings, tmp_path):
    config = replace(settings, run_root=tmp_path / "runs")
    manager = JobManager(config)
    run_id = uuid.uuid4().hex
    directory = config.run_root / run_id
    directory.mkdir()
    with manager.connect() as db:
        db.execute(
            "INSERT INTO jobs (run_id,request_id,scenario,brief,concurrency,max_repairs,"
            "status,created,updated,error_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, uuid.uuid4().hex, "csv-orders", "", 2, 1, "running", "now", "now", None),
        )
    manager.close()
    reopened = JobManager(config)
    try:
        assert reopened.get(run_id)["status"] == "interrupted"
        assert reopened.get(run_id)["error_type"] == "ServerRestart"
    finally:
        reopened.close()


def test_public_https_origin_through_loopback_proxy(settings, tmp_path):
    config = replace(settings, run_root=tmp_path / "runs")
    app = ProxyHeadersMiddleware(create_app(config), trusted_hosts=["127.0.0.1"])
    with TestClient(app, base_url="http://agent.foxbh.top", client=("127.0.0.1", 40000)) as client:
        assert client.get("/api/meta").status_code == 200
        response = client.post(
            "/api/runs",
            json={**payload(), "scenario": "invalid"},
            headers={"Origin": "https://agent.foxbh.top", "X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 422  # Origin passes; schema rejects without API use.
        assert (
            client.post(
                "/api/runs",
                json=payload(),
                headers={"Origin": "https://evil.invalid", "X-Forwarded-Proto": "https"},
            ).status_code
            == 403
        )
