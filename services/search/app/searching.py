"""一次搜索的完整流程：读缓存、主 provider、备用链、写缓存。REST /search 和 MCP 工具都调这里（MCP 不许 import routes）。

- 主 provider 报错，或者结果不到 count 的一半，按备用链顺序再试，够数即停；都不够时用条数最多的那份。
- 整次搜索有总时限（SEARCH_DEADLINE_SECONDS）：各家超时串行累加会超过网关给的 30 秒，网关回了 504，
  这边还在接着打上游。时限到了就不再试后面的备用。
- 缓存按请求的主 provider 记（读、写同一个键），备用出的结果下次同样的请求也能用上。
  结果不够数或放宽了时效的不缓存。缓存出问题只记日志，照常搜（见 app.cache）。
- restrict_domains：结果按 domain_filters 再过滤一遍（MCP 工具用）。Brave、Tavily 不支持站点限制，
  转到它们时不能混进别的网站；比条数也按过滤后的算。REST 照旧不过滤。
- Firecrawl 的 credits 由调用方记（REST 放后台任务，不拖慢响应）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.budget import record_fallback
from app.cache import get_cached, set_cached
from app.config import settings
from app.logger import get_logger
from app.models import SearchRequest, SearchResponse
from app.providers.registry import get_fallback_providers, get_provider

log = get_logger()


@dataclass
class SearchOutcome:
    response: SearchResponse
    # (provider, credits)：上游报了 credits 的每次成功调用，包括没被选中的备用结果
    credits: list[tuple[str, int]] = field(default_factory=list)


class SearchFailed(Exception):
    """主 provider 和整条备用链都没出结果。primary 是主 provider 的异常（REST 照旧抛它，状态码不变），
    errors 是每家的 (名字, 异常)，MCP 按它分类（全是额度用完、超时、上游限流……）。"""

    def __init__(self, primary: BaseException, errors: list[tuple[str, BaseException]]) -> None:
        super().__init__(error_text(primary))
        self.primary = primary
        self.errors = errors


def error_text(exc: BaseException) -> str:
    # httpx 超时等异常的 str() 是空串，日志里补上异常类型，不然无从排查。
    return str(exc) or type(exc).__name__


def domain_matches(url: str, domains: list[str]) -> bool:
    """网址属于 domains 之一（含子域名）。domains 已经是小写、不带 www. 的裸域名。"""
    try:
        host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _restricted(response: SearchResponse, domains: list[str]) -> SearchResponse:
    kept = [r for r in response.results if domain_matches(r.url, domains)]
    if len(kept) == len(response.results):
        return response
    return response.model_copy(update={"results": kept})


async def run_search(body: SearchRequest, *, restrict_domains: bool = False) -> SearchOutcome:
    provider_name = body.provider or None
    provider = get_provider(provider_name)  # 没注册的名字抛 ValueError（REST 照旧 500）
    actual_provider = provider_name or settings.SEARCH_PROVIDER
    domains = body.domain_filters if restrict_domains else []

    def shaped(response: SearchResponse) -> SearchResponse:
        return _restricted(response, domains) if domains else response

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
        return SearchOutcome(shaped(cached))

    log.info(
        "search_request",
        query=body.query,
        type=body.type.value,
        provider=actual_provider,
        freshness=body.freshness,
        count=body.count,
        page=body.page,
    )
    deadline = asyncio.get_running_loop().time() + settings.SEARCH_DEADLINE_SECONDS
    min_acceptable = max(body.count // 2, 1)
    credits: list[tuple[str, int]] = []
    try:
        async with asyncio.timeout_at(deadline):
            result = await provider.search(body)
        result = shaped(
            _with_provenance(
                result,
                requested_provider=actual_provider,
                result_provider=result.provider or actual_provider,
                fallback_used=False,
                provider_chain=[actual_provider],
            )
        )
        _note_credits(result, credits)
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
        log.warning("primary_search_failed", provider=actual_provider, error=error_text(e))
        errors: list[tuple[str, BaseException]] = [(actual_provider, e)]
        fallback_result = await _run_fallback_chain(
            body,
            actual_provider,
            reason="error",
            min_results=min_acceptable,
            deadline=deadline,
            shaped=shaped,
            credits=credits,
            errors=errors,
        )
        if fallback_result is None:
            raise SearchFailed(e, errors) from e
        result = fallback_result

    # 主 provider 成功但结果不足时走备用链；报错路径已经把整条链走过，不再重复调用。
    if len(result.results) < min_acceptable and not result.fallback_used:
        fallback_result = await _run_fallback_chain(
            body,
            actual_provider,
            reason="insufficient",
            min_results=min_acceptable,
            deadline=deadline,
            shaped=shaped,
            credits=credits,
            errors=[],
            primary_results=len(result.results),
        )
        if fallback_result and len(fallback_result.results) > len(result.results):
            result = fallback_result

    # 结果质量不足或放宽了时效约束时跳过缓存
    if len(result.results) >= min_acceptable and not result.relaxed_freshness:
        await set_cached(actual_provider, body, result)
    else:
        log.warning(
            "skip_cache",
            query=body.query,
            provider=result.provider,
            requested=body.count,
            returned=len(result.results),
            relaxed_freshness=result.relaxed_freshness,
        )
    return SearchOutcome(result, credits)


async def _run_fallback_chain(
    body: SearchRequest,
    actual_provider: str,
    *,
    reason: str,
    min_results: int,
    deadline: float,
    shaped,
    credits: list[tuple[str, int]],
    errors: list[tuple[str, BaseException]],
    primary_results: int | None = None,
) -> SearchResponse | None:
    """按备用链顺序尝试，结果够 min_results 条即停；都不够时返回条数最多的那个，全部失败返回 None。"""
    chain = get_fallback_providers(actual_provider)
    if chain:
        # 日报统计“转备用”次数，按请求计一次。直接 await 而不是放后台任务：
        # 备用全失败时请求会抛错，后台任务不会执行，这种最该记的情况就漏了。
        await record_fallback(reason)
    best: SearchResponse | None = None
    tried = [actual_provider]
    loop = asyncio.get_running_loop()
    for fb_name, fb_provider in chain:
        if loop.time() >= deadline:
            log.warning("fallback_skipped_deadline", query=body.query, provider=fb_name)
            break
        tried.append(fb_name)
        log.info(
            "fallback_search",
            query=body.query,
            primary=actual_provider,
            fallback=fb_name,
            primary_results=primary_results,
        )
        try:
            async with asyncio.timeout_at(deadline):
                fb_result = await fb_provider.search(body)
        except Exception as e:
            log.warning("fallback_failed", query=body.query, provider=fb_name, error=error_text(e))
            errors.append((fb_name, e))
            continue

        fb_result = shaped(
            _with_provenance(
                fb_result,
                requested_provider=actual_provider,
                result_provider=fb_result.provider or fb_name,
                fallback_used=True,
                provider_chain=list(tried),
            )
        )
        _note_credits(fb_result, credits)
        log.info(
            "fallback_response",
            query=body.query,
            provider=fb_name,
            result_count=len(fb_result.results),
            titles=[r.title[:50] for r in fb_result.results[:3]],
        )
        if best is None or len(fb_result.results) > len(best.results):
            best = fb_result
        if len(fb_result.results) >= min_results:
            break
    # 每个结果的 provider_chain 是“到它为止依次试过的 provider”，末尾即 result_provider。
    return best


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


def _note_credits(response: SearchResponse, credits: list[tuple[str, int]]) -> None:
    provider = response.result_provider or response.provider
    if provider != "firecrawl":
        return
    if response.credits_used is None or response.credits_used <= 0:
        return
    credits.append(("firecrawl", response.credits_used))
