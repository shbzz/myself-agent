"""Local authenticated MCP fixture, containing no production credentials."""

import asyncio
import json
import sys

import uvicorn
from mcp import types
from mcp.server import Server
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


async def list_tools(context, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=name,
                description="Offline fixture " + name,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                },
            )
            for name in ["add", "echo", "fail", "pause", "oversized"]
        ]
    )


async def call_tool(context, params):
    arguments = params.arguments or {}
    name = params.name
    if name == "pause":
        await asyncio.sleep(4)
    text = (
        json.dumps({"sum": arguments.get("a", 0) + arguments.get("b", 0)})
        if name == "add"
        else "x" * (1024 * 1024 + 1)
        if name == "oversized"
        else arguments.get("text", "fixture-error" if name == "fail" else "ok")
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=name == "fail"
    )


server = Server("offline-fixture", on_list_tools=list_tools, on_call_tool=call_tool)
app = server.streamable_http_app(json_response=True)


async def authenticate(request, call_next):
    if request.headers.get("authorization") != "Bearer fixture-credential":
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)


app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, log_level="error")
