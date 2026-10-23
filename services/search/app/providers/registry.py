import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.cache import get_redis
from app.config import settings
from app.logger import get_logger
from app.models import ProviderHistoricalUsageResponse, ProviderUsageResponse
from app.providers.base import SearchProvider
from app.providers.brave import BraveProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.firecrawl import FirecrawlProvider, FirecrawlUsageClient, FirecrawlUsageError
from app.providers.tavily import TavilyProvider
from app.usage import get_recorded_provider_usage

_providers: dict[str, SearchProvider] = {}
log = get_logger()


@dataclass(frozen=True)
class _CachedValue[T]:
    payload: T
    stale: bool


class ProviderUsageError(Exception):
    pass


def _init_providers() -> None:
    _providers["brave"] = BraveProvider()
    if settings.TAVILY_API_KEY:
        _providers["tavily"] = TavilyProvider()
    if settings.FIRECRAWL_API_KEY:
        _providers["firecrawl"] = FirecrawlProvider()
    if settings.DEEPSEEK_API_KEY:
        _providers["deepseek"] = DeepSeekProvider()


def get_provider(name: str | None = None) -> SearchProvider:
    if not _providers:
        _init_providers()
    provider_name = name or settings.SEARCH_PROVIDER
    if provider_name not in _providers:
        raise ValueError(f"Unknown provider: {provider_name}")
    return _providers[provider_name]


def get_fallback_provider(primary_name: str) -> tuple[str | None, SearchProvider | None]:
    """获取备用 provider（排除主 provider）"""
    if not _providers:
        _init_providers()
    for name, prov in _providers.items():
        if name != primary_name:
            return name, prov
    return None, None


def list_providers() -> list[dict]:
    if not _providers:
        _init_providers()
    return [{"name": name, "available": True} for name in _providers]


async def get_firecrawl_usage() -> ProviderUsageResponse:
    if not settings.FIRECRAWL_API_KEY.strip():
        return ProviderUsageResponse(provider="firecrawl", available=False)

    cache_key = "firecrawl:usage:current"
    cached = await _get_cached_usage_response(
        cache_key,
        fresh_ttl=settings.FIRECRAWL_USAGE_CACHE_TTL,
        stale_ttl=settings.FIRECRAWL_USAGE_STALE_TTL,
    )
    if cached is None:
        try:
            usage = await FirecrawlUsageClient().get_usage()
        except FirecrawlUsageError as exc:
            raise ProviderUsageError(str(exc)) from exc
        await _set_cached_usage_response(
            cache_key,
            _cache_storage_ttl(settings.FIRECRAWL_USAGE_CACHE_TTL, settings.FIRECRAWL_USAGE_STALE_TTL),
            usage,
        )
    else:
        usage = cached.payload
        if cached.stale:
            _schedule_firecrawl_usage_refresh(cache_key)

    usage.recorded_usage = await get_recorded_provider_usage(
        "firecrawl",
        period_start=usage.billing_period_start,
        period_end=usage.billing_period_end,
    )
    return usage


async def get_firecrawl_historical_usage(*, by_api_key: bool = False) -> ProviderHistoricalUsageResponse:
    if not settings.FIRECRAWL_API_KEY.strip():
        return ProviderHistoricalUsageResponse(provider="firecrawl", available=False, by_api_key=by_api_key)

    cache_key = f"firecrawl:usage:historical:by_api_key:{str(by_api_key).lower()}"
    cached = await _get_cached_historical_usage_response(
        cache_key,
        fresh_ttl=settings.FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL,
        stale_ttl=settings.FIRECRAWL_HISTORICAL_USAGE_STALE_TTL,
    )
    if cached is not None:
        if cached.stale:
            _schedule_firecrawl_historical_usage_refresh(cache_key, by_api_key=by_api_key)
        return cached.payload

    try:
        usage = await FirecrawlUsageClient().get_historical_usage(by_api_key=by_api_key)
    except FirecrawlUsageError as exc:
        raise ProviderUsageError(str(exc)) from exc

    await _set_cached_historical_usage_response(
        cache_key,
        _cache_storage_ttl(
            settings.FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL,
            settings.FIRECRAWL_HISTORICAL_USAGE_STALE_TTL,
        ),
        usage,
    )
    return usage


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _cache_storage_ttl(fresh_ttl: int, stale_ttl: int) -> int:
    return max(fresh_ttl, 0) + max(stale_ttl, 0)


async def _get_cached_usage_response(
    cache_key: str,
    *,
    fresh_ttl: int,
    stale_ttl: int,
) -> _CachedValue[ProviderUsageResponse] | None:
    try:
        redis = await get_redis()
        data = await redis.get(cache_key)
    except Exception as exc:
        log.warning("firecrawl_usage_cache_read_failed", cache_key=cache_key, error=str(exc))
        return None
    if data is None:
        return None
    try:
        cached = _parse_cached_value(
            data,
            model=ProviderUsageResponse,
            fresh_ttl=fresh_ttl,
            stale_ttl=stale_ttl,
        )
    except ValueError as exc:
        log.warning("firecrawl_usage_cache_parse_failed", cache_key=cache_key, error=str(exc))
        return None
    if cached is not None:
        cached.payload.recorded_usage = None
    return cached


async def _set_cached_usage_response(cache_key: str, ttl: int, usage: ProviderUsageResponse) -> None:
    try:
        redis = await get_redis()
        cached_usage = usage.model_copy(update={"recorded_usage": None})
        await redis.setex(cache_key, ttl, _cache_wrapper_json(cached_usage))
    except Exception as exc:
        log.warning("firecrawl_usage_cache_write_failed", cache_key=cache_key, error=str(exc))


async def _get_cached_historical_usage_response(
    cache_key: str,
    *,
    fresh_ttl: int,
    stale_ttl: int,
) -> _CachedValue[ProviderHistoricalUsageResponse] | None:
    try:
        redis = await get_redis()
        data = await redis.get(cache_key)
    except Exception as exc:
        log.warning("firecrawl_historical_usage_cache_read_failed", cache_key=cache_key, error=str(exc))
        return None
    if data is None:
        return None
    try:
        return _parse_cached_value(
            data,
            model=ProviderHistoricalUsageResponse,
            fresh_ttl=fresh_ttl,
            stale_ttl=stale_ttl,
        )
    except ValueError as exc:
        log.warning("firecrawl_historical_usage_cache_parse_failed", cache_key=cache_key, error=str(exc))
        return None


async def _set_cached_historical_usage_response(
    cache_key: str,
    ttl: int,
    usage: ProviderHistoricalUsageResponse,
) -> None:
    try:
        redis = await get_redis()
        await redis.setex(cache_key, ttl, _cache_wrapper_json(usage))
    except Exception as exc:
        log.warning("firecrawl_historical_usage_cache_write_failed", cache_key=cache_key, error=str(exc))


def _cache_wrapper_json(payload: ProviderUsageResponse | ProviderHistoricalUsageResponse) -> str:
    wrapper = {
        "fetched_at": _utc_now().isoformat(),
        "payload": payload.model_dump(mode="json"),
    }
    return json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))


def _parse_cached_value[ModelT: (ProviderUsageResponse, ProviderHistoricalUsageResponse)](
    data: str,
    *,
    model: type[ModelT],
    fresh_ttl: int,
    stale_ttl: int,
) -> _CachedValue[ModelT] | None:
    wrapper = json.loads(data)
    if not isinstance(wrapper, dict):
        raise ValueError("invalid cache wrapper")
    fetched_at = _parse_fetched_at(wrapper.get("fetched_at"))
    payload = model.model_validate(wrapper.get("payload"))
    age = max((_utc_now() - fetched_at).total_seconds(), 0)
    if age <= fresh_ttl:
        return _CachedValue(payload=payload, stale=False)
    if age <= fresh_ttl + stale_ttl:
        return _CachedValue(payload=payload, stale=True)
    return None


def _parse_fetched_at(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid fetched_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _schedule_firecrawl_usage_refresh(cache_key: str) -> None:
    asyncio.create_task(_refresh_firecrawl_usage_cache_if_unlocked(cache_key))


def _schedule_firecrawl_historical_usage_refresh(cache_key: str, *, by_api_key: bool) -> None:
    asyncio.create_task(_refresh_firecrawl_historical_usage_cache_if_unlocked(cache_key, by_api_key=by_api_key))


async def _acquire_refresh_lock(lock_key: str) -> bool:
    try:
        redis = await get_redis()
        return bool(await redis.set(lock_key, "1", ex=30, nx=True))
    except Exception as exc:
        log.warning("firecrawl_usage_refresh_lock_failed", lock_key=lock_key, error=str(exc))
        return False


async def _refresh_firecrawl_usage_cache_if_unlocked(cache_key: str) -> None:
    if await _acquire_refresh_lock(f"{cache_key}:refresh_lock"):
        await _refresh_firecrawl_usage_cache(cache_key)


async def _refresh_firecrawl_historical_usage_cache_if_unlocked(cache_key: str, *, by_api_key: bool) -> None:
    if await _acquire_refresh_lock(f"{cache_key}:refresh_lock"):
        await _refresh_firecrawl_historical_usage_cache(cache_key, by_api_key=by_api_key)


async def _refresh_firecrawl_usage_cache(cache_key: str) -> None:
    try:
        usage = await FirecrawlUsageClient().get_usage()
    except FirecrawlUsageError as exc:
        log.warning("firecrawl_usage_background_refresh_failed", error=str(exc))
        return
    await _set_cached_usage_response(
        cache_key,
        _cache_storage_ttl(settings.FIRECRAWL_USAGE_CACHE_TTL, settings.FIRECRAWL_USAGE_STALE_TTL),
        usage,
    )


async def _refresh_firecrawl_historical_usage_cache(cache_key: str, *, by_api_key: bool) -> None:
    try:
        usage = await FirecrawlUsageClient().get_historical_usage(by_api_key=by_api_key)
    except FirecrawlUsageError as exc:
        log.warning("firecrawl_historical_usage_background_refresh_failed", error=str(exc))
        return
    await _set_cached_historical_usage_response(
        cache_key,
        _cache_storage_ttl(
            settings.FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL,
            settings.FIRECRAWL_HISTORICAL_USAGE_STALE_TTL,
        ),
        usage,
    )
