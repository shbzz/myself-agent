"""Independent preview API and React workbench for general Agent sessions."""

import argparse
import asyncio
import base64
import binascii
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from myself_agent.config import DEFAULT_CONFIG, Settings
from myself_agent.general_manager import AgentManager
from myself_agent.general_model import (
    general_model_session,
    model_base_settings,
    provider_credential,
)
from myself_agent.general_settings import (
    ConfigurationError,
    MCPServerInput,
    ModelInput,
    PolicyInput,
    ProviderInput,
)
from myself_agent.jobs import BusyError
from myself_agent.mcp_tools import MCPAccess
from myself_agent.orchestration_api import router as orchestration_router
from myself_agent.workspace_files import WorkspaceFileError, WorkspaceFiles

FRONTEND = Path(__file__).parents[2] / "frontend" / "dist"


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


class WorkspaceArchiveInput(WorkspaceInput):
    data_base64: str = Field(max_length=12 * 1024 * 1024)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(default="新会话", min_length=1, max_length=120)


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(default="", max_length=16000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    reasoning_effort: str = Field(default="low", pattern=r"^[a-zA-Z0-9_-]{1,24}$")
    model_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    data_base64: str = Field(max_length=8 * 1024 * 1024)


def create_app(root, config_path=DEFAULT_CONFIG, manager_factory=AgentManager):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = manager_factory(root, config_path)
        yield
        app.state.manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(orchestration_router)
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
            limit = 65536
            if request.url.path.endswith("/attachments"):
                limit = 9 * 1024 * 1024
            elif request.url.path == "/next/api/workspaces/import":
                limit = 12 * 1024 * 1024 + 65536
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit:
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

    @app.exception_handler(ConfigurationError)
    async def invalid_configuration(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(WorkspaceFileError)
    async def invalid_workspace_file(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Validation errors must not echo a supplied credential value.
        return JSONResponse(
            {"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in error.errors()]},
            status_code=422,
        )

    def store(request):
        return request.app.state.manager.store

    @app.get("/next/health")
    def health():
        return {"status": "ok"}

    @app.get("/next/api/workspaces")
    def workspaces(request: Request):
        return {"workspaces": store(request).workspaces()}

    @app.get("/next/api/providers")
    def providers(request: Request):
        return {"providers": store(request).config.list("provider")}

    @app.post("/next/api/providers", status_code=201)
    def save_provider(body: ProviderInput, request: Request):
        return store(request).config.save_provider(body)

    @app.post("/next/api/providers/{provider_id}/test")
    def test_provider(provider_id: str, request: Request):
        db = store(request)
        provider = db.config.get("provider", provider_id)
        settings = model_base_settings(config_path, provider)
        snapshot = db.config.resolve()
        snapshot["provider"] = provider
        snapshot["profile"] = dict(snapshot["profile"], reasoning_parameter="none")
        try:
            key = provider_credential(db, provider, settings)
            with general_model_session(settings, "off", snapshot, key) as model:
                response = model.root_client.models.list()
                return {
                    "connected": True,
                    "models": [m.id for m in response.data[:100]],
                    "capabilities": "声明能力需通过实际运行验证",
                }
        except Exception as error:
            raise HTTPException(
                status_code=502, detail=f"连接失败（{type(error).__name__}），请检查地址和密钥"
            ) from None

    @app.get("/next/api/mcp/servers")
    def mcp_servers(request: Request):
        return {"servers": store(request).config.list("mcp")}

    @app.post("/next/api/mcp/servers", status_code=201)
    def save_mcp_server(body: MCPServerInput, request: Request):
        return store(request).config.save_mcp_server(body)

    @app.post("/next/api/mcp/servers/{server_id}/test")
    def test_mcp_server(server_id: str, request: Request):
        db = store(request)
        server = db.config.get("mcp", server_id)
        try:
            return MCPAccess(db, [server], Settings.proxy, 15).execute(server_id, discover=True)
        except ToolException as error:
            raise HTTPException(status_code=502, detail=str(error)) from None

    @app.get("/next/api/models")
    def models(request: Request):
        return {"models": store(request).config.list("model")}

    @app.post("/next/api/models", status_code=201)
    def save_model(body: ModelInput, request: Request):
        return store(request).config.save_model(body)

    @app.get("/next/api/configuration")
    def configuration(
        request: Request, workspace_id: str | None = None, session_id: str | None = None
    ):
        result = store(request).config.resolve(workspace_id, session_id)
        return store(request).config.public_snapshot(result)

    @app.post("/next/api/settings/{scope}/{identifier}")
    def save_settings(
        scope: Literal["global", "workspace", "session"],
        identifier: str,
        body: PolicyInput,
        request: Request,
    ):
        key = "global" if scope == "global" else scope + ":" + identifier
        return store(request).config.save_policy(key, body)

    @app.get("/next/api/sessions/{session_id}/context")
    def context_records(session_id: str, request: Request):
        return {"records": store(request).config.records(session_id)}

    @app.post("/next/api/sessions/{session_id}/compact", status_code=202)
    def compact(session_id: str, body: MessageInput, request: Request):
        return request.app.state.manager.submit(
            session_id,
            body.request_id,
            "手动压缩上下文",
            [],
            "off",
            body.model_profile_id,
            "compact",
        )

    @app.post("/next/api/workspaces", status_code=201)
    def create_workspace(body: WorkspaceInput, request: Request):
        return store(request).create_workspace(body.name)

    @app.post("/next/api/workspaces/import", status_code=201)
    def import_workspace(body: WorkspaceArchiveInput, request: Request):
        try:
            data = base64.b64decode(body.data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise WorkspaceFileError("ZIP 编码无效，请重新选择文件") from None
        return WorkspaceFiles(store(request)).import_archive(body.name, data)

    @app.get("/next/api/workspaces/{workspace_id}/files")
    def workspace_files(workspace_id: str, request: Request, path: str = Query(default="")):
        return WorkspaceFiles(store(request)).listing(workspace_id, path)

    @app.get("/next/api/workspaces/{workspace_id}/file")
    def workspace_file(workspace_id: str, request: Request, path: str):
        return WorkspaceFiles(store(request)).read(workspace_id, path, preview=True)

    @app.get("/next/api/workspaces/{workspace_id}/download")
    def download_workspace_file(workspace_id: str, request: Request, path: str):
        data = WorkspaceFiles(store(request)).read(workspace_id, path)
        filename = quote(path.rsplit("/", 1)[-1], safe="")
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename=file; filename*=UTF-8''" + filename,
            },
        )

    @app.get("/next/api/workspaces/{workspace_id}/archive")
    def download_workspace(workspace_id: str, request: Request):
        data = WorkspaceFiles(store(request)).export(workspace_id)
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="workspace-{workspace_id[:8]}.zip"',
            },
        )

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

    @app.post("/next/api/sessions/{session_id}/attachments", status_code=201)
    def upload(session_id: str, body: ImageInput, request: Request):
        try:
            data = base64.b64decode(body.data_base64, validate=True)
            return store(request).create_attachment(session_id, body.filename, data)
        except (ValueError, binascii.Error) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/next/api/attachments/{attachment_id}")
    def image(attachment_id: str, request: Request):
        db = store(request)
        item = db.attachment(attachment_id)
        return FileResponse(db.attachment_path(attachment_id), media_type=item["mime_type"])

    @app.post("/next/api/sessions/{session_id}/messages", status_code=202)
    def submit(session_id: str, body: MessageInput, request: Request):
        if not body.text.strip() and not body.attachment_ids:
            raise HTTPException(status_code=422, detail="请输入任务")
        return request.app.state.manager.submit(
            session_id,
            body.request_id,
            body.text,
            body.attachment_ids,
            body.reasoning_effort,
            body.model_profile_id,
        )

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
