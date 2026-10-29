from __future__ import annotations

import logging

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.server import (
    HTTP_CLIENT_LOGGERS,
    READ_ONLY_TOOLS,
    RedactKey,
    _configure_logging,
    build_server,
    upstream_client,
)

pytestmark = pytest.mark.anyio
KEY = "tmap-test-key"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_tmap() -> FastMCP:
    """模拟腾讯地图官方 MCP：白名单里的工具，以及不放行的 geocoder / weather / ipLocation。"""
    upstream = FastMCP("fake-tmap")

    for name in READ_ONLY_TOOLS - {"placeSuggestion", "directionTransit"}:

        @upstream.tool(name=name)
        def allowed(keyword: str = "") -> str:
            return "ok"

    @upstream.tool(name="placeSuggestion")
    def place_suggestion(keyword: str, region: str = "") -> str:
        return f"找到约100条结果\n(1){keyword}\n纬度：39.908823\n经度：116.397470"

    @upstream.tool(name="directionTransit")
    def direction_transit(from_: str, to: str) -> str:
        raise ToolError("服务暂不可用")

    for name in ("geocoder", "weather", "ipLocation"):

        @upstream.tool(name=name)
        def excluded(address: str = "") -> str:
            return "0,0"

    return upstream


async def test_only_read_only_tools_are_listed():
    async with Client(build_server(fake_tmap())) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_ONLY_TOOLS


@pytest.mark.parametrize("name", ["geocoder", "weather", "ipLocation"])
async def test_excluded_tools_cannot_be_called_even_by_name(name):
    async with Client(build_server(fake_tmap())) as client:
        with pytest.raises(ToolError, match=f"tool_not_allowed: {name}"):
            await client.call_tool(name, {"address": "北京"})


async def test_allowed_tool_results_pass_through():
    async with Client(build_server(fake_tmap())) as client:
        result = await client.call_tool("placeSuggestion", {"keyword": "天安门", "region": "北京"})
    assert result.is_error is False
    assert "经度：116.397470" in result.content[0].text


async def test_upstream_errors_pass_through_untouched():
    async with Client(build_server(fake_tmap())) as client:
        result = await client.call_tool("directionTransit", {"from_": "a", "to": "b"}, raise_on_error=False)
    assert result.is_error is True
    assert "服务暂不可用" in result.content[0].text
    assert not result.structured_content


def test_upstream_client_puts_key_in_query_and_never_forwards_caller_headers():
    client = upstream_client(KEY, url="https://mcp.map.qq.com/mcp")
    assert client.transport.url == f"https://mcp.map.qq.com/mcp?key={KEY}"
    build_server(client)
    # create_proxy 默认打开入站头转发，上游是第三方，必须关掉。
    assert client.transport.forward_incoming_headers is False


async def test_health_route_is_served_without_mcp_handshake():
    app = build_server(fake_tmap()).http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tmap") as http:
            response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_client_loggers_are_quiet_and_key_is_redacted_everywhere():
    _configure_logging(KEY)
    assert {logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS} == {logging.WARNING}

    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "POST %s", (f"https://mcp.map.qq.com/mcp?key={KEY}&format=0",), None
    )
    assert RedactKey(KEY).filter(record) is True
    assert KEY not in record.getMessage()
    assert "key=<redacted>&format=0" in record.getMessage()

    other = logging.LogRecord("x", logging.INFO, __file__, 1, "GET /x?KEY=other-secret", None, None)
    RedactKey(KEY).filter(other)
    assert "other-secret" not in other.getMessage()
    assert all(isinstance(f, RedactKey) for f in logging.getLogger().handlers[0].filters)
