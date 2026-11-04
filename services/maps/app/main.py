"""地图服务：持有高德、腾讯两把 Web 服务 key，给产品的模型提供 MCP 工具 place_search、route_compare（/mcp）。

自身不鉴权，只接入专用网络，经网关 /maps/... 访问。两家都没配 key 也能启动，工具回“查不到”（和天气一样，不让部署失败）。
原来的 amap-service、tmap-service 两个代理不动，Fusion 还在用。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

from app.amap import AmapClient
from app.config import Settings
from app.mcp_server import build_mcp
from app.tmap import TmapClient
from app.tools import Client, MapsService
from app.upstream import Gate, QuotaMemo, Usage, beijing_today

logger = logging.getLogger("maps")
# httpx 会以 INFO 打印请求行，查询参数里有 key，一律压到 WARNING
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")
# MCP 库每个请求都打一条 INFO（无状态模式下的建立、结束会话），也压到 WARNING
MCP_LOGGERS = ("mcp.server.streamable_http", "mcp.server.streamable_http_manager")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for name in (*HTTP_CLIENT_LOGGERS, *MCP_LOGGERS):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_service(
    settings: Settings, http: httpx.AsyncClient, usage: Usage, memo: QuotaMemo | None = None
) -> MapsService:
    """先高德后腾讯；没配 key 的那家不加。"""
    memo = memo or QuotaMemo()
    clients: list[Client] = []
    amap_key = settings.amap_api_key.get_secret_value().strip()
    tmap_key = settings.tmap_api_key.get_secret_value().strip()
    if amap_key:
        clients.append(AmapClient(amap_key, http, Gate("amap", memo, usage, settings.provider_concurrency)))
    if tmap_key:
        clients.append(TmapClient(tmap_key, http, Gate("tmap", memo, usage, settings.provider_concurrency)))
    if not clients:
        logger.warning("maps_not_configured: no provider key; tools will return no result")
    return MapsService(clients, usage, call_timeout=settings.call_timeout_seconds)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    resolved = settings or Settings()
    timeout = max(1.0, resolved.upstream_timeout_seconds)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        transport=transport,
    )
    usage = Usage(resolved.usage_state_path)
    service = build_service(resolved, http, usage)
    mcp_app = build_mcp(service).http_app(path="/mcp", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            await http.aclose()

    app = FastAPI(title="agent-tools maps", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "providers": service.providers}

    @app.get("/usage/daily")
    async def daily_usage(day: date | None = None) -> dict[str, Any]:
        """某天（北京时间，默认今天）各家的请求、失败、额度用尽、跳过次数和转给下一家的次数，供网关日报读取；只有计数。"""
        return usage.summary(day or beijing_today())

    app.mount("/", mcp_app)  # 放在最后：上面的路由先匹配，其余（/mcp）交给 MCP
    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
