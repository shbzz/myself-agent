"""Independent preview API and React workbench for general Agent sessions."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from myself_agent.config import DEFAULT_CONFIG
from myself_agent.general_manager import AgentManager
from myself_agent.jobs import BusyError

FRONTEND = Path(__file__).parents[2] / "frontend" / "dist"


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(default="新会话", min_length=1, max_length=120)


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(min_length=1, max_length=16000)


def create_app(root, config_path=DEFAULT_CONFIG, manager_factory=AgentManager):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = manager_factory(root, config_path)
        yield
        app.state.manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver", "agent.foxbh.top"],
    )

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.method not in ("GET", "HEAD"):
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (
                request.headers.get("origin", expected) != expected
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse({"detail": "仅允许从本工作台操作"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "需要 JSON 请求"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.exception_handler(BusyError)
    async def busy(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def store(request):
        return request.app.state.manager.store

    @app.get("/next/health")
    def health():
        return {"status": "ok"}

    @app.get("/next/api/workspaces")
    def workspaces(request: Request):
        return {"workspaces": store(request).workspaces()}

    @app.post("/next/api/workspaces", status_code=201)
    def create_workspace(body: WorkspaceInput, request: Request):
        return store(request).create_workspace(body.name)

    @app.get("/next/api/sessions")
    def sessions(request: Request, workspace_id: str):
        return {"sessions": store(request).sessions(workspace_id)}

    @app.post("/next/api/sessions", status_code=201)
    def create_session(body: SessionInput, request: Request):
        return store(request).create_session(body.workspace_id, body.title)

    @app.get("/next/api/sessions/{session_id}")
    def session(session_id: str, request: Request):
        db = store(request)
        return {
            "session": db.session(session_id),
            "messages": db.messages(session_id),
            "runs": db.runs(session_id),
        }

    @app.post("/next/api/sessions/{session_id}/messages", status_code=202)
    def submit(session_id: str, body: MessageInput, request: Request):
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="请输入任务")
        return request.app.state.manager.submit(session_id, body.request_id, body.text)

    @app.post("/next/api/runs/{run_id}/stop")
    def stop(run_id: str, request: Request):
        return request.app.state.manager.cancel(run_id)

    @app.get("/next/api/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = Query(0, ge=0)):
        db = store(request)
        db.session(session_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(status_code=422, detail="无效事件游标") from None

        async def stream():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                rows = db.events(session_id, cursor)
                for row in rows:
                    cursor = row["sequence"]
                    yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                ticks += 1
                if ticks % 30 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.3)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    if FRONTEND.is_dir():
        app.mount("/next/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/next/")
    def index():
        return FileResponse(FRONTEND / "index.html")

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.root, args.config),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )


if __name__ == "__main__":
    main()
