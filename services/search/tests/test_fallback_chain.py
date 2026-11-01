import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType


def _http_request() -> Request:
    return Request(
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


class FakeProvider:
    def __init__(self, name: str, count: int = 0, error: Exception | None = None):
        self.name = name
        self.count = count
        self.error = error
        self.calls = 0

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SearchResponse(
            query=request.query,
            type=request.type,
            provider=self.name,
            results=[
                SearchResultItem(title=f"{self.name}-{i}", url=f"https://{self.name}.example/{i}", description="d")
                for i in range(self.count)
            ],
        )


def _patch_route(monkeypatch, primary: FakeProvider, chain: list[FakeProvider]):
    from app.routes import search as search_route

    cached: list[str] = []

    async def no_cache(*_args, **_kwargs):
        return None

    async def record_cache(provider, _body, _result):
        cached.append(provider)

    monkeypatch.setattr(search_route, "get_cached", no_cache)
    monkeypatch.setattr(search_route, "set_cached", record_cache)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: primary)
    monkeypatch.setattr(search_route, "get_fallback_providers", lambda _primary: [(p.name, p) for p in chain])
    return search_route, cached


async def _search(search_route, count: int = 4) -> SearchResponse:
    body = SearchRequest(query="fusion", provider="deepseek", count=count)
    return await search_route.search.__wrapped__(_http_request(), body, BackgroundTasks())


# ---- registry：按配置顺序组链 ----


@pytest.fixture
def registry_all_keys(monkeypatch):
    from app.providers import registry

    registry._providers.clear()
    registry._warned_unavailable_fallbacks.clear()
    monkeypatch.setattr(registry.settings, "BRAVE_API_KEY", "")
    monkeypatch.setattr(registry.settings, "TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setattr(registry.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(registry.settings, "DEEPSEEK_API_KEY", "sk-test-key")
    yield registry
    registry._providers.clear()
    registry._warned_unavailable_fallbacks.clear()


def test_fallback_chain_follows_configured_order_not_registration_order(monkeypatch, registry_all_keys):
    registry = registry_all_keys
    monkeypatch.setattr(registry.settings, "SEARCH_FALLBACK_PROVIDERS", "firecrawl,tavily")

    # 注册顺序是 tavily 在前，配置要求 firecrawl 在前
    assert [name for name, _ in registry.get_fallback_providers("deepseek")] == ["firecrawl", "tavily"]


def test_fallback_chain_skips_primary_itself(monkeypatch, registry_all_keys):
    registry = registry_all_keys
    monkeypatch.setattr(registry.settings, "SEARCH_FALLBACK_PROVIDERS", "firecrawl,tavily")

    # 调用方显式指定 provider=firecrawl 时，链里不再重复调它
    assert [name for name, _ in registry.get_fallback_providers("firecrawl")] == ["tavily"]


def test_fallback_chain_skips_provider_without_key_and_warns_once(monkeypatch, registry_all_keys):
    registry = registry_all_keys
    monkeypatch.setattr(registry.settings, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(registry.settings, "SEARCH_FALLBACK_PROVIDERS", "firecrawl,tavily")
    warnings: list[str] = []
    monkeypatch.setattr(registry.log, "warning", lambda event, **kw: warnings.append(kw.get("provider")))

    assert [name for name, _ in registry.get_fallback_providers("deepseek")] == ["tavily"]
    assert [name for name, _ in registry.get_fallback_providers("deepseek")] == ["tavily"]
    assert warnings == ["firecrawl"]


def test_parse_provider_list_normalizes_and_dedupes():
    from app.config import parse_provider_list

    assert parse_provider_list(" Firecrawl , tavily,,firecrawl ") == ["firecrawl", "tavily"]
    assert parse_provider_list("") == []


def test_settings_refuse_to_start_with_unknown_fallback_provider():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(SEARCH_FALLBACK_PROVIDERS="firecrawl,taivly")


# ---- route：链上依次尝试 ----


async def test_primary_error_walks_chain_until_a_provider_succeeds(monkeypatch):
    primary = FakeProvider("deepseek", error=RuntimeError("401"))
    firecrawl = FakeProvider("firecrawl", error=TimeoutError())
    tavily = FakeProvider("tavily", count=4)
    search_route, cached = _patch_route(monkeypatch, primary, [firecrawl, tavily])

    response = await _search(search_route)

    assert response.result_provider == "tavily"
    assert response.requested_provider == "deepseek"
    assert response.fallback_used is True
    assert response.provider_chain == ["deepseek", "firecrawl", "tavily"]
    assert cached == ["tavily"]
    assert (primary.calls, firecrawl.calls, tavily.calls) == (1, 1, 1)


async def test_chain_stops_at_first_provider_with_enough_results(monkeypatch):
    primary = FakeProvider("deepseek", count=0)
    firecrawl = FakeProvider("firecrawl", count=4)
    tavily = FakeProvider("tavily", count=4)
    search_route, _cached = _patch_route(monkeypatch, primary, [firecrawl, tavily])

    response = await _search(search_route)

    assert response.result_provider == "firecrawl"
    assert response.provider_chain == ["deepseek", "firecrawl"]
    assert tavily.calls == 0


async def test_insufficient_chain_keeps_the_provider_with_most_results(monkeypatch):
    primary = FakeProvider("deepseek", count=0)
    firecrawl = FakeProvider("firecrawl", count=1)
    tavily = FakeProvider("tavily", count=0)
    search_route, cached = _patch_route(monkeypatch, primary, [firecrawl, tavily])

    response = await _search(search_route)

    # 都不够数时取条数最多的；不够数的结果不写缓存
    assert response.result_provider == "firecrawl"
    assert response.provider_chain == ["deepseek", "firecrawl"]
    assert len(response.results) == 1
    assert tavily.calls == 1
    assert cached == []


async def test_primary_error_with_thin_fallback_does_not_rerun_chain(monkeypatch):
    primary = FakeProvider("deepseek", error=RuntimeError("boom"))
    firecrawl = FakeProvider("firecrawl", count=1)
    tavily = FakeProvider("tavily", count=0)
    search_route, _cached = _patch_route(monkeypatch, primary, [firecrawl, tavily])

    response = await _search(search_route)

    assert response.result_provider == "firecrawl"
    # 报错路径已经把整条链走完，结果不足的补搜不能再把链跑一遍
    assert (firecrawl.calls, tavily.calls) == (1, 1)


async def test_all_providers_failing_reraises_primary_error(monkeypatch):
    primary = FakeProvider("deepseek", error=RuntimeError("primary down"))
    firecrawl = FakeProvider("firecrawl", error=RuntimeError("429"))
    tavily = FakeProvider("tavily", error=ValueError("tavily provider does not support image search"))
    search_route, _cached = _patch_route(monkeypatch, primary, [firecrawl, tavily])

    with pytest.raises(RuntimeError, match="primary down"):
        await _search(search_route)
    assert (firecrawl.calls, tavily.calls) == (1, 1)


async def test_image_search_falls_through_unsupported_providers(monkeypatch):
    from app.routes import search as search_route

    primary = FakeProvider("deepseek", error=ValueError("deepseek provider does not support image search"))
    firecrawl = FakeProvider("firecrawl", count=3)
    tavily = FakeProvider("tavily", error=AssertionError("不应调用"))
    _patch_route(monkeypatch, primary, [firecrawl, tavily])

    body = SearchRequest(query="猫", provider="deepseek", count=4, type=SearchType.IMAGE)
    response = await search_route.search.__wrapped__(_http_request(), body, BackgroundTasks())

    assert response.result_provider == "firecrawl"
    assert tavily.calls == 0
