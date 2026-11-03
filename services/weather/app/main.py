"""和风天气服务：持有凭证、签 JWT；给产品的模型提供 MCP 工具 get_weather（/mcp），并保留原来的城市查询与预报接口。

自身不鉴权，只接入专用网络，经网关 /weather/... 访问。响应字段已归一，调用方不接触和风原始字段与令牌。
REST /v1/... 是线上 Fusion 在用的，保持原样；新的产品接入走 /mcp
（挑城市、缓存、整理结果都在服务里，见 app/forecast.py）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.forecast import Forecaster
from app.mcp_server import build_mcp
from app.qweather import QWeatherClient, QWeatherCredentials, QWeatherError

logger = logging.getLogger("weather")
# httpx 会以 INFO 打印请求行（含专属域名与查询参数），一律压到 WARNING；Authorization 头本就不会被打印。
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")
# MCP 库每个请求都打一条 INFO（无状态模式下的建立、结束会话），也压到 WARNING。
MCP_LOGGERS = ("mcp.server.streamable_http", "mcp.server.streamable_http_manager")
# 和风 LocationID 是数字串，也接受“经度,纬度”；其他字符一律拒绝，不让调用方拼出别的路径或参数。
LOCATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.,_-]{0,63}$"
# 上游错误码 → HTTP 状态；网关原样透传给调用方。
ERROR_STATUS = {
    "upstream_timeout": 504,
    "upstream_unavailable": 502,
    "upstream_auth_failed": 502,
    "upstream_rate_limited": 429,
    "invalid_response": 502,
}


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for name in (*HTTP_CLIENT_LOGGERS, *MCP_LOGGERS):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> QWeatherClient | None:
    """五项凭证齐全才构建客户端；私钥或主机无效直接抛错，让部署失败而不是静默返回 503。"""

    credentials = QWeatherCredentials.from_settings(
        api_host=settings.qweather_api_host,
        key_id=settings.qweather_key_id,
        project_id=settings.qweather_project_id,
        developer_id=settings.qweather_developer_id,
        private_key=settings.qweather_private_key,
    )
    if credentials is None:
        logger.warning("QWeather credentials are incomplete; forecast endpoints will return 503")
        return None
    return QWeatherClient(credentials, timeout_seconds=max(1.0, settings.qweather_timeout_seconds), transport=transport)


class _NotConfigured(Exception):
    """凭证不全：接口返回 503，让调用方按“天气不可用”处理而不是当成上游故障。"""


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status_code)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    resolved = settings or Settings()
    # 凭证不全时 client 是 None，get_weather 返回 not_configured
    forecaster = Forecaster(lambda: getattr(app.state, "client", None))
    mcp_app = build_mcp(forecaster).http_app(path="/mcp", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        app.state.client = build_client(resolved, transport)
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            if app.state.client is not None:
                await app.state.client.aclose()

    app = FastAPI(title="agent-tools weather", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(QWeatherError)
    async def _qweather_error(request: Request, exc: QWeatherError) -> JSONResponse:
        logger.warning("qweather_failed path=%s code=%s", request.url.path, exc.code)
        return _error(ERROR_STATUS.get(exc.code, 502), exc.code)

    def client(request: Request) -> QWeatherClient:
        resolved_client = request.app.state.client
        if resolved_client is None:
            raise _NotConfigured
        return resolved_client

    @app.exception_handler(_NotConfigured)
    async def _not_configured(request: Request, exc: _NotConfigured) -> JSONResponse:
        return _error(503, "weather_not_configured")

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return {"status": "ok", "configured": request.app.state.client is not None}

    @app.get("/v1/cities")
    async def cities(
        request: Request,
        location: Annotated[str, Query(min_length=1, max_length=120)],
        number: Annotated[int, Query(ge=1, le=20)] = 10,
        lang: Annotated[str, Query(pattern=r"^(zh|en)$")] = "zh",
    ) -> dict[str, Any]:
        return {"cities": await client(request).lookup_city(location.strip(), number=number, lang=lang)}

    @app.get("/v1/forecast/daily")
    async def daily(
        request: Request, location_id: Annotated[str, Query(pattern=LOCATION_ID_PATTERN)]
    ) -> dict[str, Any]:
        return {"daily": await client(request).daily_forecast(location_id)}

    @app.get("/v1/forecast/hourly")
    async def hourly(
        request: Request, location_id: Annotated[str, Query(pattern=LOCATION_ID_PATTERN)]
    ) -> dict[str, Any]:
        return {"hourly": await client(request).hourly_forecast(location_id)}

    app.mount("/", mcp_app)  # 放在最后：上面的路由先匹配，其余（/mcp）交给 MCP
    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
