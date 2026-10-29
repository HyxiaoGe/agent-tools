from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.server import (
    COORDINATE_CONVERT_TOOL,
    READ_ONLY_TOOLS,
    CoordinateConverter,
    build_server,
    next_quota_reset,
    upstream_client,
)

pytestmark = pytest.mark.anyio
KEY = "amap-test-key"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_amap() -> FastMCP:
    """模拟高德官方 MCP：只读工具、唤起导航的副作用工具、额度用尽的工具。"""
    upstream = FastMCP("fake-amap")

    @upstream.tool
    def maps_geo(address: str, city: str = "") -> str:
        return json.dumps({"results": [{"location": "116.397,39.909", "address": address}]})

    @upstream.tool
    def maps_text_search(keywords: str) -> str:
        raise ToolError("USER_DAILY_QUERY_OVER_LIMIT")

    @upstream.tool
    def maps_around_search(keywords: str) -> str:
        raise ToolError("INVALID_PARAMS")

    @upstream.tool
    def maps_schema_navi(lon: str, lat: str) -> str:
        return "launched navigation"

    return upstream


def converter_with(handler) -> tuple[CoordinateConverter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return CoordinateConverter(KEY, transport=httpx.MockTransport(handle)), seen


@pytest.fixture
def converter():
    converter, _ = converter_with(lambda request: httpx.Response(200, json={"status": "1", "locations": "1,2"}))
    return converter


async def test_only_read_only_tools_are_listed(converter):
    async with Client(build_server(fake_amap(), converter)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"maps_geo", "maps_text_search", "maps_around_search", COORDINATE_CONVERT_TOOL}
    assert "maps_schema_navi" not in names
    assert names - {COORDINATE_CONVERT_TOOL} <= READ_ONLY_TOOLS


async def test_hidden_tools_cannot_be_called_even_by_name(converter):
    async with Client(build_server(fake_amap(), converter)) as client:
        with pytest.raises(ToolError, match="tool_not_allowed: maps_schema_navi"):
            await client.call_tool("maps_schema_navi", {"lon": "1", "lat": "2"})


async def test_allowed_tool_results_pass_through(converter):
    async with Client(build_server(fake_amap(), converter)) as client:
        result = await client.call_tool("maps_geo", {"address": "天安门"})
    assert result.is_error is False
    assert json.loads(result.content[0].text)["results"][0]["location"] == "116.397,39.909"


async def test_quota_exhaustion_is_marked_with_structured_error(converter):
    async with Client(build_server(fake_amap(), converter)) as client:
        result = await client.call_tool("maps_text_search", {"keywords": "咖啡"}, raise_on_error=False)
    assert result.is_error is True
    # 上游原文保留，调用方按文本识别的老逻辑继续可用。
    assert "USER_DAILY_QUERY_OVER_LIMIT" in result.content[0].text
    error = result.structured_content["error"]
    assert error["code"] == "quota_exhausted"
    assert error["upstream_code"] == "USER_DAILY_QUERY_OVER_LIMIT"
    assert error["quota_group"] == "search"
    assert datetime.fromisoformat(error["reset_at"]) > datetime.now(ZoneInfo("Asia/Shanghai"))


async def test_other_upstream_errors_pass_through_untouched(converter):
    async with Client(build_server(fake_amap(), converter)) as client:
        result = await client.call_tool("maps_around_search", {"keywords": "x"}, raise_on_error=False)
    assert result.is_error is True
    assert "INVALID_PARAMS" in result.content[0].text
    assert not result.structured_content


def test_quota_resets_on_the_first_of_next_month():
    tz = ZoneInfo("Asia/Shanghai")
    assert next_quota_reset(datetime(2026, 10, 29, 12, tzinfo=tz)) == datetime(2026, 11, 1, tzinfo=tz)
    assert next_quota_reset(datetime(2026, 12, 31, 23, 59, tzinfo=tz)) == datetime(2027, 1, 1, tzinfo=tz)


async def test_coordinate_convert_injects_key_and_returns_locations():
    converter, seen = converter_with(
        lambda request: httpx.Response(200, json={"status": "1", "info": "ok", "locations": "116.48,39.99"})
    )
    async with Client(build_server(fake_amap(), converter)) as client:
        result = await client.call_tool(COORDINATE_CONVERT_TOOL, {"locations": "116.47,39.99", "coordsys": "gps"})
    assert result.content[0].text == "116.48,39.99"
    (request,) = seen
    assert request.url.host == "restapi.amap.com"
    assert request.url.params["key"] == KEY
    assert request.url.params["coordsys"] == "gps"


@pytest.mark.parametrize("locations", ["abc", "116.47;39.99", "116.47,39.99,", ";".join(["1,2"] * 41)])
async def test_coordinate_convert_rejects_malformed_locations(locations):
    converter, seen = converter_with(lambda request: httpx.Response(200, json={"status": "1", "locations": "x"}))
    async with Client(build_server(fake_amap(), converter)) as client:
        with pytest.raises(ToolError, match="invalid_locations"):
            await client.call_tool(COORDINATE_CONVERT_TOOL, {"locations": locations})
    assert seen == []


async def test_coordinate_convert_errors_never_include_the_request_url():
    converter, _ = converter_with(lambda request: httpx.Response(200, json={"status": "0", "info": "INVALID_USER_KEY"}))
    async with Client(build_server(fake_amap(), converter)) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool(COORDINATE_CONVERT_TOOL, {"locations": "116.47,39.99"})
    assert "INVALID_USER_KEY" in str(error.value)
    assert KEY not in str(error.value)
    assert "restapi" not in str(error.value)


async def test_coordinate_convert_network_failure_is_a_tool_error():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    converter, _ = converter_with(fail)
    async with Client(build_server(fake_amap(), converter)) as client:
        with pytest.raises(ToolError, match="upstream_unreachable"):
            await client.call_tool(COORDINATE_CONVERT_TOOL, {"locations": "116.47,39.99"})


def test_upstream_client_puts_key_in_query_and_never_forwards_caller_headers(converter):
    client = upstream_client(KEY, url="https://mcp.amap.com/mcp")
    assert client.transport.url == f"https://mcp.amap.com/mcp?key={KEY}"
    build_server(client, converter)
    # create_proxy 默认打开入站头转发，上游是第三方，必须关掉。
    assert client.transport.forward_incoming_headers is False


async def test_health_route_is_served_without_mcp_handshake(converter):
    app = build_server(fake_amap(), converter).http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://amap") as http:
            response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_client_loggers_are_quiet_and_key_is_redacted_everywhere():
    from app.server import HTTP_CLIENT_LOGGERS, RedactKey, _configure_logging

    _configure_logging("secret-key-123")
    assert {logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS} == {logging.WARNING}
    assert "httpx2" in HTTP_CLIENT_LOGGERS

    redact = RedactKey("secret-key-123")
    record = logging.LogRecord(
        "httpx2",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: POST %s "HTTP/1.1 200 OK"',
        ("https://mcp.amap.com/mcp?key=secret-key-123",),
        None,
    )
    assert redact.filter(record) is True
    assert "secret-key-123" not in record.getMessage()
    assert "mcp.amap.com/mcp?key=<redacted>" in record.getMessage()

    # 密钥换了但日志里还有旧的 key= 参数时，也按参数名脱敏。
    other = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "GET /convert?locations=1,2&key=another&output=json", None, None
    )
    redact.filter(other)
    assert other.getMessage() == "GET /convert?locations=1,2&key=<redacted>&output=json"
    assert all(isinstance(f, RedactKey) for f in logging.getLogger().handlers[0].filters)
