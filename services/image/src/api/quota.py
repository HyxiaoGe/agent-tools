from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.services.quota import beijing_today, daily_summary

router = APIRouter()


@router.get("/quota")
async def quota(request: Request, day: str | None = None):
    """各上游某天（北京时间，默认今天）的调用 / 被拦次数与每日上限，供网关日报读取。

    不需要产品密钥：只有计数，不含产品、用户或提示词（与 /metrics 一样只在内网可达）。
    """
    try:
        target = date.fromisoformat(day) if day else beijing_today()
    except ValueError:
        return JSONResponse({"error": "invalid_params", "message": "Use day=YYYY-MM-DD."}, status_code=422)
    return await daily_summary(request.app.state.redis, request.app.state.settings, target)
