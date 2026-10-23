from fastapi import APIRouter, BackgroundTasks, Request

from app.cache import get_cached, set_cached
from app.config import settings
from app.limiter import limiter
from app.logger import get_logger
from app.models import SearchRequest, SearchResponse
from app.providers.registry import get_fallback_provider, get_provider
from app.usage import record_search_credits

router = APIRouter()
log = get_logger()


@router.post("/search", response_model=SearchResponse)
@limiter.limit("10/second")
async def search(
    request: Request,
    body: SearchRequest,
    background_tasks: BackgroundTasks,
) -> SearchResponse:
    provider_name = body.provider or None
    provider = get_provider(provider_name)
    actual_provider = provider_name or settings.SEARCH_PROVIDER

    cached = await get_cached(actual_provider, body)
    if cached:
        log.info(
            "cache_hit",
            query=body.query,
            type=body.type.value,
            provider=actual_provider,
            freshness=body.freshness,
            count=body.count,
            page=body.page,
            cache_key_version=3,
        )
        return cached

    log.info(
        "search_request",
        query=body.query,
        type=body.type.value,
        provider=actual_provider,
        freshness=body.freshness,
        count=body.count,
        page=body.page,
    )
    try:
        result = await provider.search(body)
        result = _with_provenance(
            result,
            requested_provider=actual_provider,
            result_provider=result.provider or actual_provider,
            fallback_used=False,
            provider_chain=[actual_provider],
        )
        _schedule_provider_credits(result, background_tasks)
        log.info(
            "search_response",
            query=body.query,
            provider=actual_provider,
            result_provider=result.result_provider,
            result_count=len(result.results),
            relaxed_freshness=result.relaxed_freshness,
            titles=[r.title[:50] for r in result.results[:3]],
        )
    except Exception as e:
        log.warning("primary_search_failed", provider=actual_provider, error=str(e))
        fallback_result = await _run_fallback_search(body, actual_provider, background_tasks=background_tasks)
        if fallback_result is None:
            raise
        result = fallback_result

    # 结果不足时尝试 fallback provider
    min_acceptable = max(body.count // 2, 1)
    if len(result.results) < min_acceptable:
        fallback_result = await _run_fallback_search(
            body,
            actual_provider,
            primary_results=len(result.results),
            background_tasks=background_tasks,
        )
        if fallback_result and len(fallback_result.results) > len(result.results):
            result = fallback_result

    # 结果质量不足或放宽了时效约束时跳过缓存
    if len(result.results) >= min_acceptable and not result.relaxed_freshness:
        cache_provider = result.result_provider or result.provider
        await set_cached(cache_provider, body, result)
    else:
        log.warning(
            "skip_cache",
            query=body.query,
            provider=result.provider,
            requested=body.count,
            returned=len(result.results),
            relaxed_freshness=result.relaxed_freshness,
        )

    return result


async def _run_fallback_search(
    body: SearchRequest,
    actual_provider: str,
    *,
    background_tasks: BackgroundTasks,
    primary_results: int | None = None,
) -> SearchResponse | None:
    fb_name, fb_provider = get_fallback_provider(actual_provider)
    if not fb_provider:
        return None

    log.info(
        "fallback_search",
        query=body.query,
        primary=actual_provider,
        fallback=fb_name,
        primary_results=primary_results,
    )
    try:
        fb_result = await fb_provider.search(body)
    except Exception as e:
        log.warning("fallback_failed", provider=fb_name, error=str(e))
        return None

    fb_result = _with_provenance(
        fb_result,
        requested_provider=actual_provider,
        result_provider=fb_result.provider or fb_name,
        fallback_used=True,
        provider_chain=[actual_provider, fb_name],
    )
    _schedule_provider_credits(fb_result, background_tasks)
    log.info(
        "fallback_response",
        query=body.query,
        provider=fb_name,
        result_count=len(fb_result.results),
        titles=[r.title[:50] for r in fb_result.results[:3]],
    )
    return fb_result


def _with_provenance(
    response: SearchResponse,
    *,
    requested_provider: str,
    result_provider: str,
    fallback_used: bool,
    provider_chain: list[str],
) -> SearchResponse:
    response.provider = result_provider
    response.requested_provider = requested_provider
    response.result_provider = result_provider
    response.fallback_used = fallback_used
    response.provider_chain = provider_chain
    response.cache_key_version = 3
    return response


def _schedule_provider_credits(response: SearchResponse, background_tasks: BackgroundTasks) -> None:
    provider = response.result_provider or response.provider
    if provider != "firecrawl":
        return
    if response.credits_used is None or response.credits_used <= 0:
        return
    background_tasks.add_task(record_search_credits, "firecrawl", response.credits_used)
