from __future__ import annotations

import logging

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.server import (
    API_KEY_HEADER,
    DOCUMENTATION_TOOLS,
    HTTP_CLIENT_LOGGERS,
    RedactKey,
    _configure_logging,
    build_server,
    upstream_client,
)

pytestmark = pytest.mark.anyio
KEY = "ctx7sk-test-key"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_context7() -> FastMCP:
    """模拟 Context7 官方 MCP：两个文档工具、一个假想的新工具、一个被限流的工具。"""
    upstream = FastMCP("fake-context7")

    @upstream.tool(name="resolve-library-id")
    def resolve_library_id(libraryName: str, query: str) -> str:
        return f"- Title: {libraryName}\n- Context7-compatible library ID: /fastapi/fastapi"

    @upstream.tool(name="query-docs")
    def query_docs(libraryId: str, query: str) -> str:
        raise ToolError("Rate limited due to too many requests. Please try again later.")

    @upstream.tool(name="future-write-tool")
    def future_write_tool(payload: str) -> str:
        return "wrote"

    return upstream


async def test_only_documentation_tools_are_listed():
    async with Client(build_server(fake_context7())) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == DOCUMENTATION_TOOLS


async def test_hidden_tools_cannot_be_called_even_by_name():
    async with Client(build_server(fake_context7())) as client:
        with pytest.raises(ToolError, match="tool_not_allowed: future-write-tool"):
            await client.call_tool("future-write-tool", {"payload": "x"})


async def test_allowed_tool_results_pass_through():
    async with Client(build_server(fake_context7())) as client:
        result = await client.call_tool("resolve-library-id", {"libraryName": "FastAPI", "query": "lifespan"})
    assert result.is_error is False
    assert "/fastapi/fastapi" in result.content[0].text


async def test_rate_limit_is_marked_with_structured_error():
    async with Client(build_server(fake_context7())) as client:
        result = await client.call_tool("query-docs", {"libraryId": "/x/y", "query": "q"}, raise_on_error=False)
    assert result.is_error is True
    assert "Rate limited" in result.content[0].text
    assert result.structured_content["error"] == {"code": "rate_limited", "tool": "query-docs"}


async def test_other_upstream_errors_pass_through_untouched():
    upstream = FastMCP("fake")

    @upstream.tool(name="query-docs")
    def query_docs(libraryId: str, query: str) -> str:
        raise ToolError("Library not found")

    async with Client(build_server(upstream)) as client:
        result = await client.call_tool("query-docs", {"libraryId": "/x/y", "query": "q"}, raise_on_error=False)
    assert result.is_error is True
    assert "Library not found" in result.content[0].text
    assert not result.structured_content


def test_upstream_client_sends_key_as_header_and_never_forwards_caller_headers():
    client = upstream_client(KEY, url="https://mcp.context7.com/mcp")
    assert client.transport.url == "https://mcp.context7.com/mcp"
    assert client.transport.headers == {API_KEY_HEADER: KEY}
    build_server(client)
    # create_proxy 默认打开入站头转发，上游是第三方，必须关掉。
    assert client.transport.forward_incoming_headers is False


def test_upstream_client_without_key_sends_no_auth_header():
    client = upstream_client("", url="https://mcp.context7.com/mcp")
    assert not client.transport.headers


async def test_health_route_is_served_without_mcp_handshake():
    app = build_server(fake_context7()).http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://context7") as http:
            response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_client_loggers_are_quiet_and_key_is_redacted():
    _configure_logging(KEY)
    assert {logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS} == {logging.WARNING}

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "sent header %s", (f"{API_KEY_HEADER}: {KEY}",), None)
    assert RedactKey(KEY).filter(record) is True
    assert KEY not in record.getMessage()
    assert "<redacted>" in record.getMessage()
    assert all(isinstance(f, RedactKey) for f in logging.getLogger().handlers[0].filters)
