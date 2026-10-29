from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth import AuthError, authenticate, authorize_service
from app.config import Settings, Upstream
from app.proxy import forward, mcp_tool_name
from app.usage import UsageStore

_LOG_FIELDS = ("service", "app_id", "path", "tool", "status", "outcome", "duration_ms", "request_id")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname.lower(), "event": record.getMessage(), "logger": record.name}
        for field in _LOG_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger = logging.getLogger("gateway")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


# 各服务的健康检查：search / reader 只在专用网络里，监控只能经网关探测。
_HEALTH_PATHS = {"image": "v1/health", "search": "health", "reader": "health", "amap": "health"}
logger = logging.getLogger("gateway")


def _is_public(service: str, path: str, method: str) -> bool:
    if method not in {"GET", "HEAD"}:
        return False
    # 生图结果图片本身就是公开地址（返回给前端直接加载），调用方按同一入口下载时不需要密钥。
    return (service == "image" and path.startswith("static/")) or _HEALTH_PATHS.get(service) == path


def _has_dot_segment(path: str) -> bool:
    # 拒绝 . / .. 路径段：否则 static/../mcp 这类路径会借公开前缀绕过鉴权。
    return any(segment in {".", ".."} for segment in path.split("/"))


def _client(upstream: Upstream, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=upstream.base_url,
        transport=transport,
        timeout=httpx.Timeout(upstream.timeout_seconds, pool=settings.pool_wait_seconds),
        limits=httpx.Limits(
            max_connections=upstream.max_connections, max_keepalive_connections=upstream.max_connections
        ),
        # 上游可能在内网走宿主代理，网关到上游一律直连。
        trust_env=False,
        follow_redirects=False,
    )


def create_app(
    config: Settings | None = None, transports: Mapping[str, httpx.AsyncBaseTransport] | None = None
) -> FastAPI:
    settings = config or Settings()
    upstreams = settings.upstreams()
    transports = transports or {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _configure_logging()
        app.state.clients = {name: _client(up, settings, transports.get(name)) for name, up in upstreams.items()}
        app.state.usage = UsageStore(settings.usage_db_path, settings.usage_retention_days)
        try:
            yield
        finally:
            for client in app.state.clients.values():
                await client.aclose()
            app.state.usage.close()

    app = FastAPI(title="agent-tools gateway", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(service: str, path: str, request: Request) -> Response:
        upstream = upstreams.get(service)
        if upstream is None:
            return JSONResponse({"error": "not_found", "message": "Unknown service."}, status_code=404)
        if _has_dot_segment(path):
            return JSONResponse({"error": "invalid_path", "message": "Path must not contain dot segments."}, 400)
        try:
            caller = None if _is_public(service, path, request.method) else authenticate(settings, request.headers)
            if caller is not None:
                authorize_service(settings, caller, service)
        except AuthError as exc:
            if exc.status_code == 403:
                logger.warning("service_not_allowed", extra={"service": service, "app_id": caller.app_id})
            headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
            return JSONResponse(
                {"error": exc.code, "message": exc.message}, status_code=exc.status_code, headers=headers
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_request_body_bytes:
                return JSONResponse(
                    {"error": "payload_too_large", "message": "Request body too large."}, status_code=413
                )
        return await forward(
            request,
            service=service,
            path=path,
            upstream=upstream,
            client=request.app.state.clients[service],
            caller=caller,
            body=bytes(body),
            tool=mcp_tool_name(path, bytes(body)),
            usage=request.app.state.usage,
        )

    return app
