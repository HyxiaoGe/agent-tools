"""腾讯地图（腾讯位置服务）官方 MCP 的代理服务：产品不直接拿腾讯密钥，经网关 /tmap/mcp 调用这里。

- 密钥只在这里注入到上游地址，调用方和日志都看不到。
- 只放行地点搜索、逆地址解析、路线与距离矩阵；geocoder（任何地址都回 0,0 且标注不可信）、
  weather（天气由 weather 服务提供）、ipLocation（定位的是服务器出口 IP）不列出、调用即拒绝。
- 上游结果原样透传，包括文本格式（format=0）；控制台没给 key 分配额度时上游不报错、只回「未找到结果」，
  这里无法区分，排查时先看控制台。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("tmap")

DEFAULT_UPSTREAM_URL = "https://mcp.map.qq.com/mcp"

# 只读工具白名单。要加时在这里加并同步 README 与 Fusion 的管理页授权。
READ_ONLY_TOOLS = frozenset(
    {
        "placeSuggestion",
        "placeSearchNearby",
        "placeDetail",
        "placeAlongby",
        "reverseGeocoder",
        "directionDriving",
        "futureDrivingDirection",
        "directionWalking",
        "directionBicycling",
        "directionTransit",
        "waypointOrder",
        "matrix",
    }
)


class TmapContract(Middleware):
    """白名单。调用方看不到的工具也调不到，不依赖列表过滤。"""

    def __init__(self, allowed_tools: frozenset[str]):
        self.allowed_tools = allowed_tools

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.name in self.allowed_tools]

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name not in self.allowed_tools:
            raise ToolError(f"tool_not_allowed: {name}")
        return await call_next(context)


def upstream_client(api_key: str, *, url: str = DEFAULT_UPSTREAM_URL, call_timeout_seconds: float = 30.0) -> Client:
    transport = StreamableHttpTransport(str(httpx.URL(url).copy_add_param("key", api_key)))
    return Client(transport, timeout=call_timeout_seconds, init_timeout=15)


def build_server(upstream: Any, *, allowed_tools: frozenset[str] = READ_ONLY_TOOLS) -> FastMCP:
    """upstream 是 fastmcp Client 或任何 create_proxy 接受的目标（测试里传内存里的 FastMCP）。"""
    proxy = create_proxy(upstream, name="tmap")
    if isinstance(upstream, Client) and isinstance(upstream.transport, StreamableHttpTransport):
        # 代理默认把调用方的 Authorization 头转发给上游，这里的上游是第三方，不能透传任何入站头。
        upstream.transport.forward_incoming_headers = False
    proxy.add_middleware(TmapContract(allowed_tools))

    @proxy.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return proxy


# fastmcp 的客户端传输用 httpx2（httpx 的分支包），INFO 级会打带密钥的完整请求地址。
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
    api_key = os.environ.get("TMAP_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("TMAP_API_KEY is required")
    _configure_logging(api_key)
    timeout = float(os.environ.get("TMAP_CALL_TIMEOUT_SECONDS", "30"))
    url = os.environ.get("TMAP_MCP_URL", DEFAULT_UPSTREAM_URL)
    server = build_server(upstream_client(api_key, url=url, call_timeout_seconds=timeout))
    server.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "3336")), show_banner=False)


if __name__ == "__main__":
    main()
