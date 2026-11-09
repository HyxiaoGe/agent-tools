"""MCP 工具 search_web：产品经网关 /search/mcp 给自己的模型用，流程见 app.searching（和 REST 同一套备用链、缓存、额度）。

说明（英文，模型看的）写在这里，Fusion 和灵稿共用；什么时候搜、搜够没有这类策略由产品接在后面，
所以这里不点名别的工具、不写工具自己的名字。规则来自两边踩过的坑：
- 不设默认时效（Fusion 146553ea：默认一周把分析类问题限死），按天数换成四档，结果里写明实际按几天搜的；
- 不开放语言、地区（Fusion f0192c89：Brave 不认 zh/cn 返回 422），语言靠查询本身；
- 不合法的域名报参数错，不悄悄丢掉（丢光了就变成不限网站，模型不知道）；限定网站时结果按域名再过滤一遍；
- 同一批去重只用来比较（www、跟踪参数、结尾斜杠、参数顺序），返回原网址和原顺序（Fusion 57bbf992→8e0e8b70）；
- 每条只给一段 snippet，最多 1000 字（Fusion c3139892）；发布时间只给上游明确给的、认得出的日期；
- 0 条是成功加空列表，失败是 {code, message, retryable}，两者不混（Fusion 755d481d）；
- 额度用完不可重试，告诉模型别再搜（Fusion 把它当临时故障，自动重试、还引导换说法再搜）。

结果：structuredContent 是整份（meta 里是 provider 链，给日志和用量；还有站点图标 favicons，给产品画来源卡）；
文字是去掉 meta 的 JSON（给模型）。
失败：structuredContent 是 {ok:false, error:{code, message, retryable}}，文字是给模型的说明；isError 保持 false，
和 weather、image 的 MCP 一样，客户端读 structuredContent。错误里不带上游原文（可能有上游网址和查询）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from app.budget import ProviderBudgetExceeded
from app.logger import get_logger
from app.models import SearchRequest, SearchResponse, SearchType
from app.searching import SearchFailed, run_search
from app.usage import record_search_credits

log = get_logger()

TOOL_NAME = "search_web"
MAX_QUERY_CHARS = 400
MAX_COUNT = 20
DEFAULT_COUNT = 10
MAX_RECENCY_DAYS = 365
MAX_DOMAINS = 5
SNIPPET_CHARS = 1000
FAVICON_MAX_CHARS = 500
BEIJING = ZoneInfo("Asia/Shanghai")
# recency_days 向上取到这几档（搜索服务只认 Brave 风格的天、周、月、年）
WINDOWS = ((1, "pd"), (7, "pw"), (31, "pm"), (365, "py"))

DESCRIPTION = f"""Search the web and get candidate sources: title, URL, a snippet, and the publication date when the source gives one.

The result is JSON:
- query: the query that was searched.
- results: candidate sources in the search engine's order, with duplicates removed. Each has title, url, snippet, and published_at when the source states it. snippet is an excerpt or summary of the page, at most {SNIPPET_CHARS} characters, often with gaps; it is not the full page. An empty list means nothing matched.
- recency_window_days, only when recency_days was given: the window actually applied, which is recency_days rounded up to 1, 7, 31, or 365 days.
- time_filter_relaxed, only when true: nothing matched the time window, so the search was repeated without it; these results may be older than requested.
- domains, only when given: the domains the results were restricted to.

Using results accurately:
- Keep each claim, number, unit, place, and time attached to the specific entity and event its source describes. Preserve aggregation scope: a total across batches, an amount per batch, an annual amount, and a cumulative amount are not interchangeable. Do not add qualifiers such as 'each', 'per year', or 'cumulative' unless the source supports them; if the scope is ambiguous or figures do not reconcile, say so. Do not fill in unreported years or infer recurring annual activity from a few dated events. A publisher or site name is not automatically the subject of the event. Do not transfer figures or dates between similar entities or events.
- A publication date is metadata about the page, not necessarily the event date. If an attribution, date, calculation basis, or requested detail is missing or ambiguous, state what is unknown; do not invent a plausible explanation. Not reported or not announced does not mean that a result or event does not exist.
- Attribute each claim to the source that made it; do not say a publisher confirmed something you only saw quoted elsewhere.

On failure the result has ok=false and an error with code, message, and retryable, and says what to do next. In every case, do not fabricate facts."""

QUERY_DESCRIPTION = (
    f"A concise, focused query for one evidence need, 1-{MAX_QUERY_CHARS} characters. Use the language of the likely "
    "original sources, not necessarily the user's language. For independent angles, make separate calls. Include "
    "dates only when they help; do not automatically add the current year or repeat a query without a new evidence "
    "objective."
)
COUNT_DESCRIPTION = (
    f"Number of results to request, 1-{MAX_COUNT}; default {DEFAULT_COUNT}. Choose based on the breadth of the "
    "evidence need. Fewer may be returned."
)
RECENCY_DESCRIPTION = (
    f"Optional: only results from the past N days, 1-{MAX_RECENCY_DAYS}. Omit for no time filter; do not exclude "
    "older original reporting needed to explain a current event. The window is rounded up to 1, 7, 31, or 365 days, "
    "so 90 means the past year."
)
DOMAINS_DESCRIPTION = (
    f"Optional: restrict results to these sites, at most {MAX_DOMAINS} bare domains such as openai.com "
    "(subdomains included), without scheme, path, or port."
)

MESSAGES = {
    "bad_arguments": "The arguments were invalid.",
    "quota_exhausted": "The search service has used up its quota for today.",
    "timeout": "The search did not finish in time.",
    "rate_limited": "The search providers rate-limited the request.",
    "upstream_error": "The search providers returned an error.",
    "not_configured": "The search service is not configured.",
}
RETRYABLE = {"timeout", "rate_limited", "upstream_error"}

# 只用来判断两条结果是不是同一页（照 Fusion source_url_identity）；返回的仍是原网址
TRACKING_QUERY_PARAMS = {
    "_hsenc",
    "_hsmi",
    "dclid",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "spm",
    "ttclid",
    "twclid",
    "vero_conv",
    "vero_id",
    "yclid",
}
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
_SEP = r"(?:\s+[-–—]\s+|\s*[|｜_]\s*)"  # 横线两边要有空格，不然“10-10-2024”也会被当成重复
_REPEAT = re.compile(rf"^(?P<t>.{{2,}}?){_SEP}(?P=t)(?={_SEP}|$)")


class ArgumentError(ValueError):
    pass


def window_for(recency_days: int | None) -> tuple[int, str] | None:
    """recency_days 向上取到的那一档：(天数, freshness)。"""
    if recency_days is None:
        return None
    return next((days, value) for days, value in WINDOWS if recency_days <= days)


def normalize_domain(raw: str) -> str | None:
    """裸域名：小写、去掉开头的 www. 和结尾的点；带协议、路径、端口、通配符的不认。"""
    d = raw.strip().lower().rstrip(".")
    while d.startswith("www."):
        d = d[4:]
    return d if _DOMAIN.fullmatch(d) else None


def parse_arguments(
    query: str, count: int, recency_days: int | None, domains: list[str] | None
) -> tuple[SearchRequest, int | None, list[str]]:
    problems = []
    text = (query or "").strip()
    if not text:
        problems.append("query is empty")
    elif len(text) > MAX_QUERY_CHARS:
        problems.append(f"query must be at most {MAX_QUERY_CHARS} characters (got {len(text)})")
    if not 1 <= count <= MAX_COUNT:
        problems.append(f"count must be 1-{MAX_COUNT} (got {count})")
    if recency_days is not None and not 1 <= recency_days <= MAX_RECENCY_DAYS:
        problems.append(
            f"recency_days must be 1-{MAX_RECENCY_DAYS}, or omitted for no time filter (got {recency_days})"
        )
    given = [d for d in domains or [] if d.strip()]
    sites = list(dict.fromkeys(n for d in given if (n := normalize_domain(d))))
    invalid = [d for d in given if normalize_domain(d) is None]
    if invalid:
        problems.append(
            f"domains must be bare domains such as openai.com (invalid: {', '.join(invalid[:MAX_DOMAINS])})"
        )
    elif len(sites) > MAX_DOMAINS:
        problems.append(f"at most {MAX_DOMAINS} domains (got {len(sites)})")
    if problems:
        raise ArgumentError("; ".join(problems))
    window = window_for(recency_days)
    request = SearchRequest(
        query=text,
        type=SearchType.WEB,
        count=count,
        freshness=window[1] if window else None,
        domain_filters=sites,
    )
    return request, window[0] if window else None, sites


def url_identity(url: str) -> str:
    """同一页的比较键（照 Fusion）：协议和域名小写、去 www. 和默认端口、去跟踪参数、参数排序、去结尾斜杠和锚点。
    不是合法的 http(s) 网址返回空串。"""
    try:
        parsed = urlsplit((url or "").strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if scheme not in {"http", "https"} or not host:
        return ""
    while host.startswith("www."):
        host = host[4:]
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_QUERY_PARAMS
    ]
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def tidy_title(title: str) -> str:
    """网页标题常把正文标题重复一遍（“A - A”“A | A - 站名”），只留一份。只认一字不差的重复。"""
    return _REPEAT.sub(r"\g<t>", title.strip(), count=1)


def _midnight(moment: datetime) -> bool:
    return (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0)


def published(value: Any) -> str | None:
    """上游明确给的、认得出的日期：ISO 8601 或 RFC 2822。“3 天前”这类相对说法不给，不推断。

    只精确到天的日期，上游常写成某个时区的零点。UTC 零点、北京时间零点的只给那一天的日期：
    DeepSeek 给的是北京日期换成 UTC（“2026-11-02T16:00:00+00:00”是北京 11-03），原样给模型会读成早一天
    （11-04 对了两篇网易文章，页面上分别是 11-03、10-30）。DeepSeek 的 provider 现在已经换成北京日期，
    这里留着兜缓存里的旧结果和别的上游。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if _midnight(moment):
        return moment.date().isoformat()
    if moment.tzinfo is not None and _midnight(beijing := moment.astimezone(BEIJING)):
        return beijing.date().isoformat()
    return moment.isoformat(timespec="minutes")


def snippet(content: str | None, description: str | None) -> str:
    text = " ".join((content or description or "").split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1].rstrip() + "…"


def favicon(value: Any) -> str | None:
    """上游给的站点图标网址；只认 http(s)，不收 data: 这类内联内容。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > FAVICON_MAX_CHARS or urlsplit(text).scheme not in {"http", "https"}:
        return None
    return text


def shape(query: str, response: SearchResponse, window_days: int | None, domains: list[str]) -> dict[str, Any]:
    """query 是这次实际搜的（去掉首尾空白后的），不用上游回的：有的上游会改写或不回。

    站点图标只放 meta.favicons（按返回的网址对应），不进给模型的文字：模型用不上，产品画来源卡要。
    """
    results: list[dict[str, Any]] = []
    favicons: dict[str, str] = {}
    seen: set[str] = set()
    for item in response.results:
        key = url_identity(item.url)
        if not key or key in seen:
            continue
        seen.add(key)
        hit: dict[str, Any] = {"title": tidy_title(item.title or ""), "url": item.url.strip()}
        if text := snippet(item.content, item.description):
            hit["snippet"] = text
        if when := published(item.published_at):
            hit["published_at"] = when
        results.append(hit)
        if icon := favicon(item.favicon):
            favicons[hit["url"]] = icon
    out: dict[str, Any] = {"ok": True, "query": query, "results": results}
    if window_days is not None:
        out["recency_window_days"] = window_days
    if response.relaxed_freshness:
        out["time_filter_relaxed"] = True
    if domains:
        out["domains"] = domains
    out["meta"] = {
        "provider": response.result_provider or response.provider,
        "provider_chain": response.provider_chain,
        "fallback_used": response.fallback_used,
        "cached": response.cached,
        "favicons": favicons,
    }
    return out


def classify(error: SearchFailed) -> str:
    excs = [e for _, e in error.errors] or [error.primary]
    if all(isinstance(e, ProviderBudgetExceeded) for e in excs):
        return "quota_exhausted"
    if any(isinstance(e, TimeoutError | httpx.TimeoutException) for e in excs):
        return "timeout"
    if any(isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429 for e in excs):
        return "rate_limited"
    return "upstream_error"


def _model_text(code: str, detail: str | None) -> str:
    if code == "bad_arguments":
        return f"No search was run: {detail}. Call again with corrected arguments."
    if code == "quota_exhausted":
        return (
            "No search was run: the search service has used up its quota for today, so further searches will fail "
            "the same way. Do not search again; answer from what you already have and say that current information "
            "could not be looked up. Do not fabricate facts."
        )
    if code in RETRYABLE:
        return (
            "The search failed temporarily. You may retry once with the same or a rephrased query; if it fails again, "
            "continue with what you have and say what could not be looked up. Do not fabricate facts."
        )
    return (
        "No search was run: the search service is unavailable. Answer from what you already have and say that "
        "current information could not be looked up. Do not fabricate facts."
    )


def failure(code: str, detail: str | None = None) -> ToolResult:
    error = {"code": code, "message": detail or MESSAGES[code], "retryable": code in RETRYABLE}
    return ToolResult(content=_model_text(code, detail), structured_content={"ok": False, "error": error})


def success(result: dict[str, Any]) -> ToolResult:
    seen = {k: v for k, v in result.items() if k != "meta"}
    return ToolResult(content=json.dumps(seen, ensure_ascii=False, separators=(",", ":")), structured_content=result)


async def search_web(query: str, count: int, recency_days: int | None, domains: list[str] | None) -> ToolResult:
    try:
        request, window_days, sites = parse_arguments(query, count, recency_days, domains)
    except ArgumentError as e:
        return failure("bad_arguments", str(e))
    try:
        outcome = await run_search(request, restrict_domains=True)
    except SearchFailed as e:
        code = classify(e)
        log.warning("search_web_failed", code=code, providers=[name for name, _ in e.errors])
        return failure(code)
    except ValueError as e:  # 主 provider 没注册：配置问题
        log.error("search_web_not_configured", error=str(e))
        return failure("not_configured")
    for provider, credits_used in outcome.credits:
        await record_search_credits(provider, credits_used)
    return success(shape(request.query, outcome.response, window_days, sites))


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
        output_schema=None,
    )
    async def search_web_tool(
        query: Annotated[str, Field(description=QUERY_DESCRIPTION)],
        count: Annotated[int, Field(description=COUNT_DESCRIPTION)] = DEFAULT_COUNT,
        recency_days: Annotated[int | None, Field(description=RECENCY_DESCRIPTION)] = None,
        domains: Annotated[list[str] | None, Field(description=DOMAINS_DESCRIPTION)] = None,
    ) -> ToolResult:
        return await search_web(query, count, recency_days, domains)
