"""MCP 工具 search_web 和共用流程（app.searching）里给它加的东西：参数检查、结果规整、限定网站、总时限、失败分类。"""

import asyncio
import json

import httpx
import pytest
from fastmcp import Client

from app import cache, searching
from app.budget import ProviderBudgetExceeded
from app.mcp import search_web as sw
from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType


def _item(url: str, title: str = "t", **kw) -> SearchResultItem:
    return SearchResultItem(title=title, url=url, description=kw.pop("description", "d"), **kw)


def _response(*items: SearchResultItem, provider: str = "deepseek", **kw) -> SearchResponse:
    return SearchResponse(query="q", type=SearchType.WEB, provider=provider, results=list(items), **kw)


class FakeProvider:
    def __init__(self, name: str, items=(), error: Exception | None = None, delay: float = 0.0):
        self.name, self.items, self.error, self.delay = name, list(items), error, delay
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _response(*self.items, provider=self.name)


@pytest.fixture
def providers(monkeypatch):
    """装上主 provider 和备用链；缓存不读、只记写了什么。返回 (setup, cached)。"""
    cached: list[tuple[str, SearchRequest, SearchResponse]] = []

    async def no_cache(*_args):
        return None

    async def record_cache(provider, body, result):
        cached.append((provider, body, result))

    async def no_credits(*_args):
        return None

    monkeypatch.setattr(searching, "get_cached", no_cache)
    monkeypatch.setattr(searching, "set_cached", record_cache)
    monkeypatch.setattr(sw, "record_search_credits", no_credits)
    monkeypatch.setattr(searching.settings, "SEARCH_PROVIDER", "deepseek")

    def setup(primary: FakeProvider, *chain: FakeProvider):
        monkeypatch.setattr(searching, "get_provider", lambda _name=None: primary)
        monkeypatch.setattr(searching, "get_fallback_providers", lambda _p: [(p.name, p) for p in chain])

    return setup, cached


async def _call(**args):
    """经 MCP 调一次（内存里的 Client），返回 (给模型的文字, structuredContent, isError)。"""
    from app.mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool_mcp(sw.TOOL_NAME, args)
    return result.content[0].text, result.structuredContent, result.isError


# ---- 参数 ----


@pytest.mark.parametrize(
    ("days", "window"),
    [(1, (1, "pd")), (2, (7, "pw")), (7, (7, "pw")), (14, (31, "pm")), (31, (31, "pm")), (90, (365, "py"))],
)
def test_recency_rounds_up_to_a_window(days, window):
    assert sw.window_for(days) == window


def test_domains_are_normalized_and_deduplicated():
    request, window, sites = sw.parse_arguments(
        " openai ", 5, None, ["WWW.OpenAI.com", "openai.com.", "docs.python.org"]
    )
    assert request.query == "openai"
    assert request.domain_filters == sites == ["openai.com", "docs.python.org"]
    assert request.freshness is None and window is None


@pytest.mark.parametrize(
    "args",
    [
        {"query": "  "},
        {"query": "x" * 401},
        {"query": "q", "count": 0},
        {"query": "q", "count": 21},
        {"query": "q", "recency_days": 0},
        {"query": "q", "recency_days": 366},
        {"query": "q", "domains": ["https://openai.com"]},
        {"query": "q", "domains": ["openai.com/blog"]},
        {"query": "q", "domains": ["*.openai.com"]},
        {"query": "q", "domains": ["openai.com:443"]},
        {"query": "q", "domains": ["localhost"]},
        {"query": "q", "domains": [f"site{i}.com" for i in range(6)]},
    ],
)
async def test_bad_arguments_are_reported_not_fixed(providers, args):
    setup, _ = providers
    primary = FakeProvider("deepseek", [_item("https://a.com/1")])
    setup(primary)
    text, data, is_error = await _call(**args)
    assert is_error is False  # 和 weather 一样：失败也是正常结果，客户端读 structuredContent
    assert data["ok"] is False
    assert data["error"]["code"] == "bad_arguments" and data["error"]["retryable"] is False
    assert text.startswith("No search was run:") and "corrected arguments" in text
    assert primary.requests == []  # 不替模型改参数、不去搜


# ---- 结果规整 ----


def test_shape_dedupes_by_identity_but_keeps_original_urls_and_order():
    response = _response(
        _item("https://www.example.com/a/?utm_source=x&b=2&a=1", "Alpha - Alpha"),
        _item("http://example.com/a?a=1&b=2", "dup: http, no www, other param order"),
        _item("https://example.com/a?a=1&b=2&fbclid=z#top", "dup: tracking param and anchor"),
        _item("ftp://example.com/file", "not http"),
        _item("not a url", "garbage"),
        _item("https://example.com/b", "Beta | Beta - Site"),
    )
    out = sw.shape("q", response, None, [])
    # 协议不同不算同一页（只合并 www、跟踪参数、参数顺序、结尾斜杠、锚点）
    assert [r["url"] for r in out["results"]] == [
        "https://www.example.com/a/?utm_source=x&b=2&a=1",
        "http://example.com/a?a=1&b=2",
        "https://example.com/b",
    ]
    assert out["results"][0]["title"] == "Alpha"
    assert out["results"][2]["title"] == "Beta - Site"


def test_shape_snippet_prefers_content_and_is_capped():
    long = "字" * 1500
    out = sw.shape(
        "q",
        _response(
            _item("https://a.com/1", content="  body\n text ", description="desc"),
            _item("https://a.com/2", content=None, description="only desc"),
            _item("https://a.com/3", content=long),
            _item("https://a.com/4", content="", description=""),
        ),
        None,
        [],
    )
    snippets = [r.get("snippet") for r in out["results"]]
    assert snippets[:2] == ["body text", "only desc"]
    assert len(snippets[2]) == sw.SNIPPET_CHARS and snippets[2].endswith("…")
    assert snippets[3] is None  # 没有就不给这个字段


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("2026-06-24", "2026-06-24"),
        ("2026-06-01T00:00:00+00:00", "2026-06-01"),
        ("2026-06-01T08:30:00Z", "2026-06-01T08:30+00:00"),
        ("Wed, 15 Oct 2025 07:00:00 GMT", "2025-10-15T07:00+00:00"),
        ("3 days ago", None),
        ("", None),
        (None, None),
    ],
)
def test_published_at_only_when_recognisable(raw, shown):
    out = sw.shape("q", _response(_item("https://a.com/1", published_at=raw)), None, [])
    assert out["results"][0].get("published_at") == shown


def test_shape_reports_window_relaxation_and_domains_and_hides_meta_from_model():
    response = _response(
        _item("https://openai.com/x"),
        relaxed_freshness=True,
        result_provider="firecrawl",
        provider_chain=["deepseek", "firecrawl"],
        fallback_used=True,
    )
    out = sw.shape("q", response, 7, ["openai.com"])
    assert out["recency_window_days"] == 7
    assert out["time_filter_relaxed"] is True
    assert out["domains"] == ["openai.com"]
    assert out["meta"] == {
        "provider": "firecrawl",
        "provider_chain": ["deepseek", "firecrawl"],
        "fallback_used": True,
        "cached": False,
    }
    seen = json.loads(sw.success(out).content[0].text)
    assert "meta" not in seen and seen["results"][0]["url"] == "https://openai.com/x"


# ---- 经 MCP 调用 ----


async def test_search_web_end_to_end(providers):
    setup, cached = providers
    primary = FakeProvider("deepseek", [_item(f"https://a.com/{i}", content=f"c{i}") for i in range(5)])
    setup(primary)
    text, data, is_error = await _call(query="  latest news ", count=5, recency_days=14)
    assert is_error is False and data["ok"] is True
    assert primary.requests[0].query == "latest news"
    assert primary.requests[0].freshness == "pm" and primary.requests[0].count == 5
    assert len(data["results"]) == 5 and data["recency_window_days"] == 31
    assert data["meta"]["provider"] == "deepseek"
    seen = json.loads(text)
    assert seen["query"] == "latest news" and "meta" not in seen
    assert cached and cached[0][0] == "deepseek"


async def test_tool_is_listed_with_description_and_read_only_hints():
    from app.mcp.server import mcp

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    tool = tools[sw.TOOL_NAME]
    assert tool.description == sw.DESCRIPTION
    assert set(tool.inputSchema["properties"]) == {"query", "count", "recency_days", "domains"}
    assert tool.inputSchema["required"] == ["query"]
    assert tool.annotations.readOnlyHint is True
    assert {"search", "search_news", "search_images"} <= set(tools)  # 早先的工具还在


async def test_empty_results_are_success_not_failure(providers):
    setup, _ = providers
    setup(FakeProvider("deepseek"), FakeProvider("firecrawl"))
    text, data, _ = await _call(query="nothing")
    assert data == {**data, "ok": True, "results": []}
    assert json.loads(text)["results"] == []


# ---- 限定网站 ----


async def test_domains_filter_results_from_providers_that_ignore_them(providers):
    setup, _ = providers
    # 主 provider 只给了 1 条（要 4 条，不够一半）；备用不支持站点限制，给了 4 条别的网站
    primary = FakeProvider("deepseek", [_item("https://openai.com/a"), _item("https://evil.example/x")])
    fallback = FakeProvider(
        "tavily", [_item(f"https://other{i}.com/") for i in range(3)] + [_item("https://help.openai.com/b")]
    )
    setup(primary, fallback)
    _, data, _ = await _call(query="q", count=4, domains=["openai.com"])
    # 比条数按过滤后的算：主 1 条、备用 1 条，不替换
    assert [r["url"] for r in data["results"]] == ["https://openai.com/a"]
    assert fallback.requests[0].domain_filters == ["openai.com"]


async def test_domains_filter_cached_results_too(monkeypatch, providers):
    setup, _ = providers
    setup(FakeProvider("deepseek"))

    async def cached_unfiltered(*_args):  # 同一个键下 REST 存的是没过滤的
        return _response(_item("https://openai.com/a"), _item("https://elsewhere.com/b"), cached=True)

    monkeypatch.setattr(searching, "get_cached", cached_unfiltered)
    _, data, _ = await _call(query="q", domains=["openai.com"])
    assert [r["url"] for r in data["results"]] == ["https://openai.com/a"]
    assert data["meta"]["cached"] is True


async def test_rest_route_still_does_not_filter_by_domain(providers):
    from fastapi import BackgroundTasks

    from app.routes import search as search_route
    from tests.test_fallback_chain import _http_request

    setup, _ = providers
    setup(FakeProvider("deepseek", [_item("https://openai.com/a"), _item("https://elsewhere.com/b")]))
    body = SearchRequest(query="q", count=2, domain_filters=["openai.com"])
    response = await search_route.search.__wrapped__(_http_request(), body, BackgroundTasks())
    assert len(response.results) == 2


# ---- 失败分类 ----


def _upstream(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example/search?q=secret")
    return httpx.HTTPStatusError(
        "boom https://api.example/search?q=secret", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.parametrize(
    ("errors", "code", "retryable"),
    [
        (
            [
                ProviderBudgetExceeded("deepseek", 1000, "2026-11-04"),
                ProviderBudgetExceeded("firecrawl", 500, "2026-11-04"),
            ],
            "quota_exhausted",
            False,
        ),
        ([httpx.ReadTimeout("slow"), ProviderBudgetExceeded("firecrawl", 500, "2026-11-04")], "timeout", True),
        ([_upstream(429), _upstream(500)], "rate_limited", True),
        ([_upstream(500), ProviderBudgetExceeded("firecrawl", 500, "2026-11-04")], "upstream_error", True),
    ],
)
async def test_failures_are_classified(providers, errors, code, retryable):
    setup, _ = providers
    setup(
        FakeProvider("deepseek", error=errors[0]), *[FakeProvider(f"fb{i}", error=e) for i, e in enumerate(errors[1:])]
    )
    text, data, is_error = await _call(query="q")
    assert is_error is False
    assert data["ok"] is False and data["error"]["code"] == code and data["error"]["retryable"] is retryable
    assert "secret" not in text and "secret" not in json.dumps(data)  # 上游原文（网址、查询）不给模型
    if code == "quota_exhausted":
        assert "Do not search again" in text


async def test_unknown_primary_provider_is_not_configured(monkeypatch, providers):
    providers[0](FakeProvider("deepseek"))

    def unknown(_name=None):
        raise ValueError("Unknown provider: brave")

    monkeypatch.setattr(searching, "get_provider", unknown)
    _, data, _ = await _call(query="q")
    assert data["error"] == {"code": "not_configured", "message": sw.MESSAGES["not_configured"], "retryable": False}


async def test_deadline_stops_the_chain(monkeypatch, providers):
    setup, _ = providers
    monkeypatch.setattr(searching.settings, "SEARCH_DEADLINE_SECONDS", 0.05)
    fallback = FakeProvider("firecrawl", [_item("https://a.com/1")])
    setup(FakeProvider("deepseek", [_item("https://a.com/0")], delay=1.0), fallback)
    _, data, _ = await _call(query="q")
    assert data["error"]["code"] == "timeout"
    assert fallback.requests == []  # 时限到了，不再试备用


# ---- 缓存出错不影响搜索 ----


async def test_cache_errors_are_logged_and_ignored(monkeypatch):
    async def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get_redis", broken)
    body = SearchRequest(query="q")
    assert await cache.get_cached("deepseek", body) is None
    await cache.set_cached("deepseek", body, _response())  # 不抛


async def test_corrupt_cache_entry_is_a_miss(monkeypatch):
    class Redis:
        async def get(self, _key):
            return "{not json"

    async def fake():
        return Redis()

    monkeypatch.setattr(cache, "get_redis", fake)
    assert await cache.get_cached("deepseek", SearchRequest(query="q")) is None


# ---- 早先的 MCP 工具也走共用流程 ----


async def test_legacy_search_tool_uses_fallback_and_skips_caching_thin_results(providers):
    from app.mcp.server import mcp

    setup, cached = providers
    fallback = FakeProvider("firecrawl", [_item("https://a.com/1")])
    setup(FakeProvider("deepseek", error=_upstream(500)), fallback)
    async with Client(mcp) as client:
        result = await client.call_tool_mcp("search", {"query": "q", "count": 4})
    assert result.structuredContent["provider"] == "firecrawl" and result.structuredContent["fallback_used"] is True
    assert cached == []  # 1 条不到 4 条的一半：不缓存（原来 MCP 无条件写，会污染 REST 的缓存）
