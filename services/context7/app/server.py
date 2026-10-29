"""Context7 官方 MCP 的代理服务：产品不直接拿 Context7 密钥，经网关 /context7/mcp 调用这里。

- 密钥只在这里以 CONTEXT7_API_KEY 请求头发给上游，调用方和日志都看不到；没配密钥时按匿名额度调用。
- 只放行文档查询工具 resolve-library-id / query-docs；上游日后新增的工具不列出、调用即拒绝。
- 上游按密钥限流，被限流时保留上游原文，并附上结构化的 rate_limited 错误，调用方据此降级。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("context7")

DEFAULT_UPSTREAM_URL = "https://mcp.context7.com/mcp"
API_KEY_HEADER = "CONTEXT7_API_KEY"

# 文档查询工具白名单。要加时在这里加并同步 README 与 Fusion 的 provider_profiles。
DOCUMENTATION_TOOLS = frozenset({"resolve-library-id", "query-docs"})

_RATE_LIMIT_PATTERN = re.compile(r"(?i)\b(rate.?limit\w*|too many requests)\b|\b429\b")


def rate_limit_message(content: list[Any]) -> str | None:
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and _RATE_LIMIT_PATTERN.search(text):
            return text
    return None


class Context7Contract(Middleware):
    """白名单与限流标记。调用方看不到的工具也调不到，不依赖列表过滤。"""

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
        if not result.is_error or rate_limit_message(result.content) is None:
            return result
        logger.warning("context7_rate_limited", extra={"tool": name})
        return ToolResult(
            content=result.content,
            structured_content={"error": {"code": "rate_limited", "tool": name}},
            is_error=True,
        )


def upstream_client(api_key: str, *, url: str = DEFAULT_UPSTREAM_URL, call_timeout_seconds: float = 45.0) -> Client:
    transport = StreamableHttpTransport(url, headers={API_KEY_HEADER: api_key} if api_key else None)
    return Client(transport, timeout=call_timeout_seconds, init_timeout=20)


def build_server(upstream: Any, *, allowed_tools: frozenset[str] = DOCUMENTATION_TOOLS) -> FastMCP:
    """upstream 是 fastmcp Client 或任何 create_proxy 接受的目标（测试里传内存里的 FastMCP）。"""
    proxy = create_proxy(upstream, name="context7")
    if isinstance(upstream, Client) and isinstance(upstream.transport, StreamableHttpTransport):
        # 代理默认把调用方的 Authorization 头转发给上游，这里的上游是第三方，不能透传任何入站头。
        upstream.transport.forward_incoming_headers = False
    proxy.add_middleware(Context7Contract(allowed_tools))

    @proxy.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return proxy


# fastmcp 的客户端传输用 httpx2（httpx 的分支包），INFO 级会打完整请求行。
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")


class RedactKey(logging.Filter):
    """兜底：任何日志里出现密钥本身都替换掉，不依赖各库的日志级别。"""

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._api_key:
            message = message.replace(self._api_key, "<redacted>")
        record.msg = message
        record.args = None
        return True


def _configure_logging(api_key: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactKey(api_key))


def main() -> None:
    api_key = os.environ.get("CONTEXT7_API_KEY", "").strip()
    _configure_logging(api_key)
    if not api_key:
        logger.warning("CONTEXT7_API_KEY is not set; calling Context7 with anonymous rate limits")
    timeout = float(os.environ.get("CONTEXT7_CALL_TIMEOUT_SECONDS", "45"))
    url = os.environ.get("CONTEXT7_MCP_URL", DEFAULT_UPSTREAM_URL)
    server = build_server(upstream_client(api_key, url=url, call_timeout_seconds=timeout))
    server.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "3335")), show_banner=False)


if __name__ == "__main__":
    main()
