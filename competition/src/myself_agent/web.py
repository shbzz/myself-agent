import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from myself_agent.config import DEFAULT_CONFIG, load_settings
from myself_agent.jobs import BusyError, JobManager
from myself_agent.scenarios import SCENARIOS

STATIC = Path(__file__).parent / "static"


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    scenario: Literal["csv-orders", "text-insights", "order-report"]
    brief: str = Field(default="", max_length=1500)
    concurrency: Literal[1, 2] = 2
    max_repairs: int = Field(default=2, ge=0, le=3)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    parent_run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def create_app(settings=None, manager_factory=JobManager, config_path=DEFAULT_CONFIG):
    @asynccontextmanager
    async def lifespan(app):
        app.state.jobs = manager_factory(settings or load_settings(config_path), config_path)
        yield
        app.state.jobs.close()

    app = FastAPI(
        title="Myself Agent 编排工作台",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver", "agent.foxbh.top", "dsh.foxbh.top"],
    )

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (origin is not None and origin != expected) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse({"detail": "仅允许从本工作台发起操作"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "需要 JSON 请求"}, status_code=415)
            if int(request.headers.get("content-length", "0")) > 8192:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BusyError)
    async def busy(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "找不到该运行记录"}, status_code=404)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta():
        return {
            "model": "deepseek-flash",
            "provider": "DeepSeek 官方",
            "scenarios": [scenario.public() for scenario in SCENARIOS.values()],
            "max_active_runs": 1,
            "max_task_concurrency": 2,
        }

    @app.get("/api/runs")
    def runs(request: Request):
        return {"runs": request.app.state.jobs.list()}

    @app.get("/api/conversations")
    def conversations(request: Request):
        return {"conversations": request.app.state.jobs.conversations()}

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, request: Request):
        return request.app.state.jobs.conversation(conversation_id)

    @app.post("/api/runs", status_code=202)
    def submit(body: CreateRun, request: Request):
        return request.app.state.jobs.submit(body.model_dump())

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str, request: Request):
        return request.app.state.jobs.details(run_id)

    @app.get("/api/runs/{run_id}/download")
    def download(run_id: str, request: Request):
        if len(run_id) != 32:
            raise HTTPException(status_code=404)
        content = request.app.state.jobs.archive(run_id)
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="myself-agent-{run_id[:8]}.zip"'
            },
        )

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "chat.html")

    return app


def main():
    parser = argparse.ArgumentParser(
        description="Private home-hosted Agent orchestration workbench"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    uvicorn.run(
        create_app(config_path=args.config),
        host="127.0.0.1",
        port=args.port,
        workers=1,
        # The dedicated SSH tunnel connects through loopback. Trust only that proxy hop.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )


if __name__ == "__main__":
    main()
