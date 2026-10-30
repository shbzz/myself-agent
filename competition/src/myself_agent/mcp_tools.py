"""Explicit, versioned MCP Streamable HTTP connections; no host process execution."""

import asyncio
import ipaddress
import json
import threading
from urllib.parse import urlsplit

import httpx2
from langchain_core.tools import ToolException
from mcp import types
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

MAX_RESPONSE = 1024 * 1024


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ToolException("MCP 响应超过 1 MiB，已停止读取")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class BoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, proxy):
        self.inner = httpx2.AsyncHTTPTransport(proxy=proxy, trust_env=False, retries=0)

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            await response.aclose()
            raise ToolException("MCP 返回了不支持的压缩内容")
        response.stream = BoundedStream(response.stream)
        return response

    async def aclose(self):
        await self.inner.aclose()


class MCPAccess:
    def __init__(self, store, servers, proxy, timeout, max_calls=20):
        self.store, self.proxy, self.timeout = store, proxy, timeout
        self.servers = {server["id"]: server for server in servers}
        self.calls, self.max_calls, self.lock = 0, max_calls, threading.Lock()

    def execute(self, server_id, *, tool_name=None, arguments=None, discover=False):
        server = self.servers.get(server_id)
        if not server:
            raise ToolException("此 MCP 服务未在本次运行中启用")
        if tool_name is not None and tool_name not in server["allowed_tools"]:
            raise ToolException("该 MCP 工具不在管理员设置的白名单中")
        if len(json.dumps(arguments or {}, ensure_ascii=False)) > 16000:
            raise ToolException("MCP 调用参数不能超过 16000 字符")
        with self.lock:
            if self.calls >= self.max_calls:
                raise ToolException("本轮 MCP 操作额度已耗尽")
            self.calls += 1
        reference = server.get("credential_ref")
        try:
            secret = (self.store.root / "credentials" / reference).read_text() if reference else ""
        except OSError:
            raise ToolException("MCP 密钥不可用，请重新保存服务配置") from None
        try:
            value = asyncio.run(self.request(server, secret, tool_name, arguments, discover))
            serialized = json.dumps(value, ensure_ascii=False)
            if secret:
                serialized = serialized.replace(secret, "[credential redacted]").replace(
                    json.dumps(secret, ensure_ascii=False)[1:-1], "[credential redacted]"
                )
            if tool_name is None:
                if len(serialized) > 32000:
                    raise ToolException("MCP 工具描述或参数结构过大，请缩小工具白名单")
                return json.loads(serialized)
            return {
                "server_id": server_id,
                "tool_name": tool_name,
                "untrusted_content": serialized[:12000],
                "truncated": len(serialized) > 12000,
            }
        except ToolException:
            raise
        except Exception as error:

            def tool_error(value):
                if isinstance(value, ToolException):
                    return value
                if isinstance(value, BaseExceptionGroup):
                    for child in value.exceptions:
                        found = tool_error(child)
                        if found:
                            return found
                return None

            actionable = tool_error(error)
            if actionable:
                raise actionable from None
            raise ToolException(
                f"MCP 连接或调用失败（{type(error).__name__}），请检查服务和配置"
            ) from None

    async def request(self, server, secret, tool_name, arguments, discover):
        host = urlsplit(server["url"]).hostname
        local = host == "localhost" or bool(host and host.endswith((".local", ".localhost")))
        try:
            local = local or ipaddress.ip_address(host).is_private
        except ValueError:
            pass
        proxy = None if local else self.proxy
        headers = {"Accept-Encoding": "identity"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        # The SDK limits individual SSE events; this transport also bounds JSON
        # responses and cumulative bytes without enabling retries of tool calls.
        async with (
            asyncio.timeout(self.timeout),
            httpx2.AsyncClient(
                headers=headers,
                transport=BoundedTransport(proxy),
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
            ) as http,
        ):
            async with streamable_http_client(
                server["url"], http_client=http, max_sse_event_size=MAX_RESPONSE
            ) as transport:
                async with ClientSession(*transport, read_timeout_seconds=self.timeout) as client:
                    await client.initialize()
                    if tool_name is not None:
                        result = await client.call_tool(tool_name, arguments=arguments or {})
                        data = result.model_dump(mode="json", by_alias=True)
                        if result.is_error:
                            text = json.dumps(data, ensure_ascii=False)[:4000]
                            if secret:
                                text = text.replace(secret, "[credential redacted]").replace(
                                    json.dumps(secret, ensure_ascii=False)[1:-1],
                                    "[credential redacted]",
                                )
                            raise ToolException("MCP 工具返回错误：" + text)
                        return data
                    tools, cursor, seen = [], None, 0
                    for _ in range(3):
                        result = await client.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
                        )
                        for tool in result.tools:
                            seen += 1
                            if seen > 100:
                                raise ToolException("MCP 工具列表超过 100 项，请缩小服务范围")
                            data = tool.model_dump(mode="json", by_alias=True)
                            if discover or tool.name in server["allowed_tools"]:
                                tools.append(
                                    {k: data.get(k) for k in ("name", "description", "inputSchema")}
                                )
                        cursor = result.next_cursor
                        if not cursor:
                            return {"server_id": server["id"], "tools": tools}
                    raise ToolException("MCP 工具分页过多，请缩小服务范围")
