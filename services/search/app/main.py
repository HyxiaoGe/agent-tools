import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.budget import ProviderBudgetExceeded
from app.cache import close_redis
from app.config import settings
from app.daily_report import run_daily_report_loop
from app.limiter import limiter
from app.logger import setup_logging
from app.mcp.server import mcp
from app.routes import admin, search

mcp_app = mcp.http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # 配了日报 webhook 才起定时任务；单 worker 部署，发送去重靠 Redis 标记，重启不会重复发。
    report_task = asyncio.create_task(run_daily_report_loop()) if settings.DAILY_REPORT_WEBHOOK.strip() else None
    try:
        async with mcp_app.lifespan(app):
            yield
    finally:
        if report_task is not None:
            report_task.cancel()
            with suppress(asyncio.CancelledError):
                await report_task
        await close_redis()


app = FastAPI(title="Search Service", version="0.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def _budget_exceeded_handler(_request: Request, exc: ProviderBudgetExceeded) -> JSONResponse:
    # 主 provider 和 fallback 都没额度时才会走到这里：明确返回 503，而不是 500。
    return JSONResponse(status_code=503, content={"detail": str(exc), "provider": exc.provider})


app.add_exception_handler(ProviderBudgetExceeded, _budget_exceeded_handler)

app.include_router(search.router)
app.include_router(admin.router)

app.mount("/", mcp_app)
