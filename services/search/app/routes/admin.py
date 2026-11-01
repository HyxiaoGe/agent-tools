from fastapi import APIRouter, HTTPException

from app.budget import get_budget_status
from app.cache import flush_cache
from app.models import ProviderBudgetStatus, ProviderHistoricalUsageResponse, ProviderUsageResponse
from app.providers.registry import (
    ProviderUsageError,
    get_firecrawl_historical_usage,
    get_firecrawl_usage,
    list_providers,
)

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/providers")
async def providers() -> list[dict]:
    return list_providers()


@router.get("/usage/budget", response_model=list[ProviderBudgetStatus])
async def budget_usage() -> list[ProviderBudgetStatus]:
    """各付费 provider 今日（北京时间）已用 / 上限 / 被拒次数。"""
    return await get_budget_status()


@router.get("/usage/firecrawl", response_model=ProviderUsageResponse)
async def firecrawl_usage() -> ProviderUsageResponse:
    try:
        return await get_firecrawl_usage()
    except ProviderUsageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/usage/firecrawl/historical", response_model=ProviderHistoricalUsageResponse)
async def firecrawl_historical_usage(by_api_key: bool = False) -> ProviderHistoricalUsageResponse:
    try:
        return await get_firecrawl_historical_usage(by_api_key=by_api_key)
    except ProviderUsageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/cache")
async def clear_cache() -> dict:
    count = await flush_cache()
    return {"flushed": count}
