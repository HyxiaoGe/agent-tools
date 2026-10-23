import httpx
import pytest
from fastapi import BackgroundTasks

from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType


def _firecrawl_client(response_data: dict, calls: list[dict]):
    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            return httpx.Response(200, json=response_data, request=httpx.Request("POST", url))

    return FakeAsyncClient


def test_search_request_defaults():
    req = SearchRequest(query="test")
    assert req.type == SearchType.WEB
    assert req.count == 10
    assert req.page == 1
    assert req.lang == "en"


def test_search_response_serialization():
    resp = SearchResponse(
        query="test",
        type=SearchType.WEB,
        provider="brave",
        cached=False,
        results=[
            SearchResultItem(
                title="Test",
                url="https://example.com",
                description="A test result",
            )
        ],
    )
    data = resp.model_dump()
    assert data["query"] == "test"
    assert len(data["results"]) == 1
    assert data["results"][0]["published_at"] is None


def test_cache_key_changes_when_result_shaping_params_change():
    from app.cache import build_cache_key

    base = SearchRequest(query="fusion", count=5, freshness="pw", page=1)
    monthly = SearchRequest(query="fusion", count=5, freshness="pm", page=1)
    more = SearchRequest(query="fusion", count=10, freshness="pw", page=1)
    second_page = SearchRequest(query="fusion", count=5, freshness="pw", page=2)
    filtered = SearchRequest(query="fusion", count=5, freshness="pw", page=1, domain_filters=["example.com"])

    assert build_cache_key("brave", base) != build_cache_key("brave", monthly)
    assert build_cache_key("brave", base) != build_cache_key("brave", more)
    assert build_cache_key("brave", base) != build_cache_key("brave", second_page)
    assert build_cache_key("brave", base) != build_cache_key("brave", filtered)


def test_cache_key_distinguishes_default_region_from_explicit_region():
    from app.cache import build_cache_key

    default_region = SearchRequest(query="2026年中国结婚人数 是否创新低")
    explicit_us = SearchRequest(query="2026年中国结婚人数 是否创新低", region="us")

    assert build_cache_key("firecrawl", default_region) != build_cache_key("firecrawl", explicit_us)


async def test_cached_response_clears_credits_used(monkeypatch):
    from app import cache

    class FakeRedis:
        async def get(self, _key: str):
            return SearchResponse(
                query="fusion",
                type=SearchType.WEB,
                provider="firecrawl",
                credits_used=7,
                results=[SearchResultItem(title="one", url="https://one.example", description="one")],
            ).model_dump_json()

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(cache, "get_redis", fake_get_redis)

    response = await cache.get_cached("firecrawl", SearchRequest(query="fusion", count=1))

    assert response is not None
    assert response.cached is True
    assert response.credits_used is None


async def test_search_route_marks_fallback_provider_provenance(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        def __init__(self, response: SearchResponse):
            self.response = response

        async def search(self, request: SearchRequest) -> SearchResponse:
            return self.response

    primary_response = SearchResponse(
        query="fusion",
        type=SearchType.WEB,
        provider="brave",
        results=[
            SearchResultItem(title="one", url="https://one.example", description="one"),
        ],
    )
    fallback_response = SearchResponse(
        query="fusion",
        type=SearchType.WEB,
        provider="tavily",
        results=[
            SearchResultItem(title="one", url="https://one.example", description="one"),
            SearchResultItem(title="two", url="https://two.example", description="two"),
            SearchResultItem(title="three", url="https://three.example", description="three"),
        ],
    )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    cached_calls = []

    async def fake_set_cached(*args):
        cached_calls.append(args)

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: Provider(primary_response))
    monkeypatch.setattr(
        search_route,
        "get_fallback_provider",
        lambda _primary: ("tavily", Provider(fallback_response)),
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="fusion", provider="brave", count=4)

    response = await search_route.search.__wrapped__(request, body, BackgroundTasks())

    assert response.provider == "tavily"
    assert response.requested_provider == "brave"
    assert response.result_provider == "tavily"
    assert response.fallback_used is True
    assert response.provider_chain == ["brave", "tavily"]
    assert cached_calls[0][0] == "tavily"


async def test_search_route_records_firecrawl_credits_from_primary_response(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        async def search(self, request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                type=request.type,
                provider="firecrawl",
                credits_used=7,
                results=[
                    SearchResultItem(title="one", url="https://one.example", description="one"),
                    SearchResultItem(title="two", url="https://two.example", description="two"),
                ],
            )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    async def fake_set_cached(*_args):
        return None

    scheduled: list[tuple[object, tuple]] = []

    class FakeBackgroundTasks:
        def add_task(self, fn, *args):
            scheduled.append((fn, args))

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: Provider())
    monkeypatch.setattr(search_route, "get_fallback_provider", lambda _primary: (None, None))

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="fusion", provider="firecrawl", count=2)

    response = await search_route.search.__wrapped__(request, body, FakeBackgroundTasks())

    assert response.credits_used == 7
    assert scheduled == [(search_route.record_search_credits, ("firecrawl", 7))]


async def test_search_route_records_firecrawl_credits_even_when_later_fallback_wins(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        def __init__(self, response: SearchResponse):
            self.response = response

        async def search(self, request: SearchRequest) -> SearchResponse:
            return self.response

    primary_response = SearchResponse(
        query="fusion",
        type=SearchType.WEB,
        provider="firecrawl",
        credits_used=7,
        results=[SearchResultItem(title="one", url="https://one.example", description="one")],
    )
    fallback_response = SearchResponse(
        query="fusion",
        type=SearchType.WEB,
        provider="brave",
        results=[
            SearchResultItem(title="one", url="https://one.example", description="one"),
            SearchResultItem(title="two", url="https://two.example", description="two"),
            SearchResultItem(title="three", url="https://three.example", description="three"),
        ],
    )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    async def fake_set_cached(*_args):
        return None

    scheduled: list[tuple[object, tuple]] = []

    class FakeBackgroundTasks:
        def add_task(self, fn, *args):
            scheduled.append((fn, args))

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: Provider(primary_response))
    monkeypatch.setattr(
        search_route,
        "get_fallback_provider",
        lambda _primary: ("brave", Provider(fallback_response)),
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="fusion", provider="firecrawl", count=4)

    response = await search_route.search.__wrapped__(request, body, FakeBackgroundTasks())

    assert response.provider == "brave"
    assert scheduled == [(search_route.record_search_credits, ("firecrawl", 7))]


async def test_search_route_uses_fallback_when_primary_provider_raises(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        def __init__(self, response: SearchResponse | None = None, error: Exception | None = None):
            self.response = response
            self.error = error

        async def search(self, request: SearchRequest) -> SearchResponse:
            if self.error:
                raise self.error
            assert self.response is not None
            return self.response

    fallback_response = SearchResponse(
        query="fusion",
        type=SearchType.WEB,
        provider="brave",
        results=[
            SearchResultItem(title="one", url="https://one.example", description="one"),
            SearchResultItem(title="two", url="https://two.example", description="two"),
        ],
    )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    cached_calls = []

    async def fake_set_cached(*args):
        cached_calls.append(args)

    scheduled: list[tuple[object, tuple]] = []

    class FakeBackgroundTasks:
        def add_task(self, fn, *args):
            scheduled.append((fn, args))

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(
        search_route, "get_provider", lambda _name=None: Provider(error=TimeoutError("primary timeout"))
    )
    monkeypatch.setattr(
        search_route,
        "get_fallback_provider",
        lambda _primary: ("brave", Provider(fallback_response)),
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="fusion", provider="firecrawl", count=2)

    response = await search_route.search.__wrapped__(request, body, FakeBackgroundTasks())

    assert response.provider == "brave"
    assert response.requested_provider == "firecrawl"
    assert response.result_provider == "brave"
    assert response.fallback_used is True
    assert response.provider_chain == ["firecrawl", "brave"]
    assert cached_calls[0][0] == "brave"
    assert scheduled == []


async def test_search_route_skips_cache_for_relaxed_freshness_results(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        async def search(self, request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                type=request.type,
                provider="firecrawl",
                relaxed_freshness=True,
                results=[
                    SearchResultItem(title="one", url="https://one.example", description="one"),
                    SearchResultItem(title="two", url="https://two.example", description="two"),
                ],
            )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    cached_calls = []

    async def fake_set_cached(*args):
        cached_calls.append(args)

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: Provider())
    monkeypatch.setattr(search_route, "get_fallback_provider", lambda _primary: (None, None))

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="2026年中国结婚人数 是否创新低", provider="firecrawl", count=2, freshness="pw")

    response = await search_route.search.__wrapped__(request, body, BackgroundTasks())

    assert response.relaxed_freshness is True
    assert cached_calls == []


async def test_search_route_does_not_fallback_when_relaxed_firecrawl_results_are_sufficient(monkeypatch):
    from starlette.requests import Request

    from app.routes import search as search_route

    class Provider:
        async def search(self, request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                type=request.type,
                provider="firecrawl",
                relaxed_freshness=True,
                results=[
                    SearchResultItem(title="one", url="https://one.example", description="one"),
                    SearchResultItem(title="two", url="https://two.example", description="two"),
                    SearchResultItem(title="three", url="https://three.example", description="three"),
                ],
            )

    async def fake_get_cached(*_args, **_kwargs):
        return None

    cached_calls = []
    fallback_calls = []

    async def fake_set_cached(*args):
        cached_calls.append(args)

    def fake_get_fallback_provider(primary: str):
        fallback_calls.append(primary)
        return None, None

    monkeypatch.setattr(search_route, "get_cached", fake_get_cached)
    monkeypatch.setattr(search_route, "set_cached", fake_set_cached)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: Provider())
    monkeypatch.setattr(search_route, "get_fallback_provider", fake_get_fallback_provider)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    body = SearchRequest(query="2026年中国结婚人数 是否创新低", provider="firecrawl", count=5, freshness="pw")

    response = await search_route.search.__wrapped__(request, body, BackgroundTasks())

    assert response.result_provider == "firecrawl"
    assert response.relaxed_freshness is True
    assert fallback_calls == []
    assert cached_calls == []


def test_registry_registers_firecrawl_when_key_is_present(monkeypatch):
    from app.providers import registry

    registry._providers.clear()
    monkeypatch.setattr(registry.settings, "SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(registry.settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(registry.settings, "FIRECRAWL_API_KEY", "fc-test-key")

    providers = registry.list_providers()

    assert {"name": "firecrawl", "available": True} in providers
    assert registry.get_provider("firecrawl").api_key == "fc-test-key"
    registry._providers.clear()


async def test_firecrawl_web_response_parsing(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _firecrawl_client(
            {
                "creditsUsed": 3,
                "data": {
                    "web": [
                        {
                            "title": "Fusion",
                            "url": "https://example.com/fusion",
                            "description": "摘要",
                            "markdown": "# 正文",
                            "publishedDate": "2026-06-23",
                            "favicon": "https://example.com/favicon.ico",
                        }
                    ]
                }
            },
            calls,
        ),
    )

    response = await firecrawl.FirecrawlProvider().search(SearchRequest(query="fusion", count=2))

    assert response.provider == "firecrawl"
    assert response.type == SearchType.WEB
    assert response.credits_used == 3
    assert response.results == [
        SearchResultItem(
            title="Fusion",
            url="https://example.com/fusion",
            description="摘要",
            content="# 正文",
            published_at="2026-06-23",
            favicon="https://example.com/favicon.ico",
        )
    ]
    assert calls[0]["url"] == "https://api.firecrawl.dev/v2/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer fc-test-key"
    assert calls[0]["json"] == {
        "query": "fusion",
        "limit": 2,
        "sources": ["web"],
        "country": "US",
    }


async def test_firecrawl_news_response_parses_snippet_and_date(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _firecrawl_client(
            {
                "data": {
                    "news": [
                        {
                            "title": "News",
                            "url": "https://news.example/article",
                            "snippet": "新闻摘要",
                            "date": "2026-06-24",
                            "favicon": "https://news.example/favicon.ico",
                        }
                    ]
                }
            },
            calls,
        ),
    )

    response = await firecrawl.FirecrawlProvider().search(SearchRequest(query="fusion", type=SearchType.NEWS, count=1))

    assert response.provider == "firecrawl"
    assert response.results == [
        SearchResultItem(
            title="News",
            url="https://news.example/article",
            description="新闻摘要",
            content="新闻摘要",
            published_at="2026-06-24",
            favicon="https://news.example/favicon.ico",
        )
    ]


async def test_firecrawl_image_response_parsing(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _firecrawl_client(
            {
                "data": {
                    "images": [
                        {
                            "title": "Image",
                            "imageUrl": "https://cdn.example/image.png",
                            "url": "https://example.com/page",
                        }
                    ]
                }
            },
            calls,
        ),
    )

    response = await firecrawl.FirecrawlProvider().search(SearchRequest(query="fusion", type=SearchType.IMAGE, count=1))

    assert response.provider == "firecrawl"
    assert response.results == [
        SearchResultItem(
            title="Image",
            url="https://cdn.example/image.png",
            description="https://example.com/page",
        )
    ]
    assert calls[0]["json"]["sources"] == ["images"]


async def test_firecrawl_defaults_chinese_queries_to_cn_region(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", _firecrawl_client({"data": {"web": []}}, calls))

    await firecrawl.FirecrawlProvider().search(SearchRequest(query="2026年中国结婚人数 是否创新低", count=5))

    assert calls[0]["json"]["country"] == "CN"


async def test_firecrawl_keeps_explicit_us_region_for_chinese_queries(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", _firecrawl_client({"data": {"web": []}}, calls))

    await firecrawl.FirecrawlProvider().search(
        SearchRequest(query="2026年中国结婚人数 是否创新低", count=5, region="us")
    )

    assert calls[0]["json"]["country"] == "US"


async def test_firecrawl_retries_without_freshness_when_first_response_is_empty(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"creditsUsed": 2, "data": {"web": []}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={
                    "creditsUsed": 4,
                    "data": {
                        "web": [
                            {
                                "title": "结婚人数",
                                "url": "https://example.cn/marriage",
                                "description": "统计信息",
                            }
                        ]
                    }
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    response = await firecrawl.FirecrawlProvider().search(
        SearchRequest(query="2026年中国结婚人数 是否创新低", count=5, freshness="pw")
    )

    assert len(response.results) == 1
    assert response.relaxed_freshness is True
    assert response.credits_used == 6
    assert calls[0]["json"]["tbs"] == "qdr:w"
    assert "tbs" not in calls[1]["json"]
    assert calls[1]["json"]["country"] == "CN"


async def test_firecrawl_keeps_empty_first_response_when_relaxed_retry_fails(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"creditsUsed": 2, "data": {"web": []}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(500, json={"error": "temporary"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    response = await firecrawl.FirecrawlProvider().search(
        SearchRequest(query="2026年中国结婚人数 是否创新低", count=5, freshness="pw")
    )

    assert response.results == []
    assert response.relaxed_freshness is False
    assert response.credits_used == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("freshness", "expected_tbs"),
    [
        ("pd", "qdr:d"),
        ("pw", "qdr:w"),
        ("pm", "qdr:m"),
        ("py", "qdr:y"),
        ("cdr:1,cd_min:06/01/2026", "cdr:1,cd_min:06/01/2026"),
    ],
)
async def test_firecrawl_freshness_and_domain_params_map(monkeypatch, freshness: str, expected_tbs: str):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", _firecrawl_client({"data": {"news": []}}, calls))

    await firecrawl.FirecrawlProvider().search(
        SearchRequest(
            query="fusion",
            type=SearchType.NEWS,
            count=3,
            lang="zh",
            region="ca",
            freshness=freshness,
            domain_filters=["example.com", "openai.com"],
        )
    )

    assert calls[0]["json"] == {
        "query": "fusion",
        "limit": 3,
        "sources": ["news"],
        "country": "CA",
        "tbs": expected_tbs,
        "includeDomains": ["example.com", "openai.com"],
    }
