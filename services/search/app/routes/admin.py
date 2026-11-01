from datetime import date

from fastapi import APIRouter, HTTPException

from app.budget import FALLBACK_REASONS, budget_day, get_budget_status, get_fallback_counts
from app.cache import flush_cache
from app.config import parse_daily_limits, parse_provider_list, settings
from app.logger import get_logger
from app.models import (
    DailyProviderUsage,
    DailyUsageSummary,
    ProviderBudgetStatus,
    ProviderHistoricalUsageResponse,
    ProviderUsageResponse,
)
from app.providers.registry import (
    ProviderUsageError,
    get_firecrawl_historical_usage,
    get_firecrawl_usage,
    list_providers,
)

router = APIRouter()
log = get_logger()


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


@router.get("/usage/daily", response_model=DailyUsageSummary)
async def daily_usage(day: date | None = None) -> DailyUsageSummary:
    """某天（北京时间，默认今天）各 provider 的上游调用、被额度拦下次数、转备用次数，附 Firecrawl 当前剩余 credits。"""
    day_str = day.isoformat() if day else budget_day()
    statuses = {s.provider: s for s in await get_budget_status(day_str)}
    available = all(s.available for s in statuses.values())
    try:
        fallback = await get_fallback_counts(day_str)
    except Exception as exc:
        log.warning("daily_usage_fallback_counts_failed", error=str(exc) or type(exc).__name__)
        available = False
        fallback = dict.fromkeys(FALLBACK_REASONS, 0)

    registered = {item["name"] for item in list_providers()}
    fallback_chain = parse_provider_list(settings.SEARCH_FALLBACK_PROVIDERS)
    limits = parse_daily_limits(settings.PROVIDER_DAILY_LIMITS)
    # 主 provider 在前，接着按备用链顺序，其余按字母序
    preferred = [settings.SEARCH_PROVIDER, *fallback_chain]
    names = set(registered) | set(limits)
    ordered = [n for n in dict.fromkeys(preferred) if n in names] + sorted(n for n in names if n not in preferred)

    providers = []
    for name in ordered:
        status = statuses.get(name)
        role = "primary" if name == settings.SEARCH_PROVIDER else "fallback" if name in fallback_chain else "other"
        providers.append(
            DailyProviderUsage(
                provider=name,
                role=role,
                registered=name in registered,
                limit=limits.get(name),
                used=status.used if status else 0,
                rejected=status.rejected if status else 0,
            )
        )

    summary = DailyUsageSummary(day=day_str, available=available, providers=providers, fallback=fallback)
    if "firecrawl" in registered:
        try:
            usage = await get_firecrawl_usage()
            summary.firecrawl_remaining_credits = usage.remaining_credits
            summary.firecrawl_billing_period_end = usage.billing_period_end
        except Exception as exc:
            log.warning("daily_usage_firecrawl_failed", error=str(exc) or type(exc).__name__)
    return summary


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
