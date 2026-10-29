"""高德官方 MCP 的代理服务：产品不直接拿高德密钥，经网关 /amap/mcp 调用这里。

- 密钥只在这里注入到上游地址，调用方和日志都看不到。
- 只放行只读工具（地理编码、搜索、路线、距离）；唤起导航等有副作用或运维用途的工具不列出、调用即拒绝。
- 高德个人开发者按服务组计月配额，用尽时上游返回 USER_DAILY_QUERY_OVER_LIMIT（名字叫 DAILY，实际
  北京时间每月 1 日零点重置）。这里保留上游原文，并附上结构化的 quota_exhausted 错误和重置时间，
  调用方据此在重置前隐藏依赖该额度的工具。
- 额外提供 maps_coordinate_convert：高德 Web 服务的坐标转换（设备 WGS84 → GCJ02），
  官方 MCP 没有这个工具，但做周边搜索、路线规划离不开它。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("amap")

DEFAULT_UPSTREAM_URL = "https://mcp.amap.com/mcp"
COORDINATE_CONVERT_URL = "https://restapi.amap.com/v3/assistant/coordinate/convert"
COORDINATE_CONVERT_TOOL = "maps_coordinate_convert"

# 只读工具白名单。maps_schema_*（唤起高德 App）、maps_weather 等没列入；要加时在这里加并同步 README。
READ_ONLY_TOOLS = frozenset(
    {
        "maps_geo",
        "maps_regeocode",
        "maps_direction_bicycling",
        "maps_direction_walking",
        "maps_direction_driving",
        "maps_direction_transit_integrated",
        "maps_distance",
        "maps_text_search",
        "maps_around_search",
        "maps_search_detail",
    }
)
# 搜索类接口共用一份月配额；其余按工具各自计。
SEARCH_QUOTA_TOOLS = frozenset({"maps_text_search", "maps_around_search", "maps_search_detail"})
SEARCH_QUOTA_GROUP = "search"

_QUOTA_ERROR_PATTERN = re.compile(r"\b(USER_DAILY_QUERY_OVER_LIMIT|DAILY_QUERY_OVER_LIMIT)\b")
_RESET_TZ = ZoneInfo("Asia/Shanghai")
# 一组 "经度,纬度"，最多 40 对（高德接口上限）。
_LOCATIONS_PATTERN = re.compile(
    r"^[+-]?\d{1,3}(?:\.\d{1,8})?,[+-]?\d{1,2}(?:\.\d{1,8})?(?:[;|][+-]?\d{1,3}(?:\.\d{1,8})?,[+-]?\d{1,2}(?:\.\d{1,8})?){0,39}$"
)
_MAX_LOCATIONS_CHARS = 2000
CoordSys = Literal["gps", "mapbar", "baidu", "autonavi"]


def quota_group(tool_name: str) -> str:
    return SEARCH_QUOTA_GROUP if tool_name in SEARCH_QUOTA_TOOLS else tool_name


def next_quota_reset(now: datetime | None = None) -> datetime:
    """高德月配额在北京时间每月 1 日零点重置。"""
    current = (now or datetime.now(_RESET_TZ)).astimezone(_RESET_TZ)
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (month_start + timedelta(days=32)).replace(day=1)


def quota_error_code(content: list[Any]) -> str | None:
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            match = _QUOTA_ERROR_PATTERN.search(text)
            if match:
                return match.group(1)
    return None


class AmapContract(Middleware):
    """白名单与配额标记。调用方看不到的工具也调不到，不依赖列表过滤。"""

    def __init__(self, allowed_tools: frozenset[str]):
        self.allowed_tools = allowed_tools

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.name in self.allowed_tools]

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name not in self.allowed_tools:
            raise ToolError(f"tool_not_allowed: {name}")
        result = await call_next(context)
        if not result.is_error:
            return result
        code = quota_error_code(result.content)
        if code is None:
            return result
        group = quota_group(name)
        logger.warning("amap_quota_exhausted", extra={"tool": name, "quota_group": group})
        return ToolResult(
            content=result.content,
            structured_content={
                "error": {
                    "code": "quota_exhausted",
                    "upstream_code": code,
                    "quota_group": group,
                    "reset_at": next_quota_reset().isoformat(),
                }
            },
            is_error=True,
        )


class CoordinateConverter:
    """高德 Web 服务坐标转换；密钥只在发请求时拼进查询串，报错不带地址。"""

    def __init__(
        self, api_key: str, *, timeout_seconds: float = 8.0, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport, follow_redirects=False)

    async def convert(self, locations: str, coordsys: CoordSys) -> str:
        if len(locations) > _MAX_LOCATIONS_CHARS or not _LOCATIONS_PATTERN.fullmatch(locations.strip()):
            raise ToolError("invalid_locations: expected up to 40 'lng,lat' pairs separated by ';'")
        try:
            response = await self._client.get(
                COORDINATE_CONVERT_URL,
                params={"key": self._api_key, "locations": locations.strip(), "coordsys": coordsys},
            )
        except httpx.HTTPError as error:
            raise ToolError(f"upstream_unreachable: {type(error).__name__}") from None
        if response.status_code != 200:
            raise ToolError(f"upstream_error: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ToolError("upstream_error: invalid response") from None
        ok = isinstance(payload, dict) and payload.get("status") == "1" and isinstance(payload.get("locations"), str)
        if not ok:
            info = payload.get("info") if isinstance(payload, dict) else None
            raise ToolError(f"upstream_error: {info if isinstance(info, str) else 'unknown'}")
        return payload["locations"]

    async def aclose(self) -> None:
        await self._client.aclose()


def upstream_client(api_key: str, *, url: str = DEFAULT_UPSTREAM_URL, call_timeout_seconds: float = 30.0) -> Client:
    transport = StreamableHttpTransport(httpx.URL(url).copy_add_param("key", api_key).__str__())
    return Client(transport, timeout=call_timeout_seconds, init_timeout=15)


def build_server(
    upstream: Any,
    converter: CoordinateConverter,
    *,
    allowed_tools: frozenset[str] = READ_ONLY_TOOLS,
) -> FastMCP:
    """upstream 是 fastmcp Client 或任何 create_proxy 接受的目标（测试里传内存里的 FastMCP）。"""
    proxy = create_proxy(upstream, name="amap")
    if isinstance(upstream, Client) and isinstance(upstream.transport, StreamableHttpTransport):
        # 代理默认把调用方的 Authorization 头转发给上游，这里的上游是第三方，不能透传任何入站头。
        upstream.transport.forward_incoming_headers = False
    proxy.add_middleware(AmapContract(allowed_tools | {COORDINATE_CONVERT_TOOL}))

    @proxy.tool(
        name=COORDINATE_CONVERT_TOOL,
        description=(
            "Convert coordinates to AMap's GCJ-02 system. Use before maps_around_search / maps_direction_* when the "
            "coordinates come from a device GPS (WGS-84, coordsys=gps) or another map provider. `locations` is up to "
            "40 'lng,lat' pairs separated by ';'. Returns the converted pairs in the same order."
        ),
        annotations={"readOnlyHint": True},
    )
    async def maps_coordinate_convert(locations: str, coordsys: CoordSys = "gps") -> str:
        return await converter.convert(locations, coordsys)

    @proxy.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return proxy


# fastmcp 的客户端传输用 httpx2（httpx 的分支包），坐标转换用 httpx；两套都会以 INFO 打完整请求地址。
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")
_KEY_QUERY = re.compile(r"(?i)(\bkey=)[^&\s\"'<>]+")


class RedactKey(logging.Filter):
    """兜底：任何日志里出现密钥本身或 key= 查询参数都替换掉，不依赖各库的日志级别。"""

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    def _clean(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "<redacted>")
        return _KEY_QUERY.sub(r"\1<redacted>", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._clean(record.getMessage())
        record.args = None
        return True


def _configure_logging(api_key: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactKey(api_key))


def main() -> None:
    api_key = os.environ.get("AMAP_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("AMAP_API_KEY is required")
    _configure_logging(api_key)
    timeout = float(os.environ.get("AMAP_CALL_TIMEOUT_SECONDS", "30"))
    url = os.environ.get("AMAP_MCP_URL", DEFAULT_UPSTREAM_URL)
    server = build_server(
        upstream_client(api_key, url=url, call_timeout_seconds=timeout),
        CoordinateConverter(api_key, timeout_seconds=min(timeout, 8.0)),
    )
    server.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "3334")), show_banner=False)


if __name__ == "__main__":
    main()
