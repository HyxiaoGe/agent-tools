from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import generate_latest

from src import __version__
from src.api.generate import validation_exception_handler
from src.api.router import v1_router
from src.config import Settings
from src.mcp.owned import init_owned_mcp, owned_mcp
from src.mcp.server import init_mcp, mcp
from src.middleware.auth import CallerAuthMiddleware
from src.middleware.disconnect import DisconnectMiddleware
from src.services.cache import CacheService
from src.services.image_store import ImageStore
from src.services.usage import usage_store


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer()
            if os.getenv("ENV", "dev") == "dev"
            else structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", level=logging.INFO)


def image_store(settings: Settings) -> ImageStore:
    """/v2/mcp 出的图：私有目录和记录（默认在 STORAGE_DIR 的上一级，和用量库一样在 ./storage 卷里）。"""
    base = Path(settings.storage_dir).parent
    return ImageStore(settings.owned_storage_dir or base / "owned", settings.image_db_path or base / "images.sqlite3")


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()

    if settings is None:
        settings = Settings()

    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
    v2_app = owned_mcp.http_app(path="/mcp", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        # Startup
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        cache_service = CacheService(redis_client, ttl=settings.cache_ttl_seconds)
        app.state.settings = settings
        app.state.redis = redis_client
        app.state.cache = cache_service
        app.state.mcp = mcp
        if settings.usage_enabled:
            path = settings.usage_db_path or str(Path(settings.storage_dir).parent / "usage.sqlite3")
            await usage_store(path, settings.usage_retention_days).ping()
        init_mcp(settings, cache_service, http_client=getattr(app.state, "http_client", None))
        app.state.image_store = image_store(settings)
        init_owned_mcp(
            settings, cache_service, app.state.image_store, http_client=getattr(app.state, "http_client", None)
        )
        storage_path = Path(settings.storage_dir)
        storage_path.mkdir(parents=True, exist_ok=True)
        async with mcp_app.lifespan(app), v2_app.lifespan(app):
            yield
        # Shutdown
        await redis_client.aclose()

    app = FastAPI(
        title="Image Service",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],  # Explicit: no open CORS by default
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.add_middleware(CallerAuthMiddleware, settings=settings)
    app.add_middleware(DisconnectMiddleware)

    # Static file serving for generated images
    storage_path = Path(settings.storage_dir)
    storage_path.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.static_url_prefix,
        StaticFiles(directory=str(storage_path)),
        name="images",
    )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")

    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.include_router(v1_router, prefix="/v1")

    # Mount MCP servers：/v2/mcp（图存在服务里、按 id 取）要挂在 "/" 前面，否则被旧的那个吃掉
    app.mount("/v2", v2_app)
    app.mount("/", mcp_app)

    return app
