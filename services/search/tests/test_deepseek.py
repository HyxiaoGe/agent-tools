from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.models import SearchRequest, SearchType


def _deepseek_client(response_data: dict, calls: list[dict]):
    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def post(self, url: str, json: dict, headers: dict):
            calls.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, json=response_data, request=httpx.Request("POST", url))

    return FakeAsyncClient


@pytest.mark.asyncio
async def test_deepseek_maps_domains_and_freshness_into_query_operators(monkeypatch):
    from app.providers import deepseek

    calls: list[dict] = []
    monkeypatch.setattr(deepseek.settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", _deepseek_client({"results": []}, calls))

    provider = deepseek.DeepSeekProvider()
    await provider.search(
        SearchRequest(query=" 发布会 ", count=5, freshness="pw", domain_filters=["github.com", "huggingface.co"])
    )

    after = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    assert calls[0]["url"] == "https://api.deepseek.com/v1/tools/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"] == {
        "query": f"发布会 site:github.com OR site:huggingface.co after:{after}",
        "count": 5,
    }


@pytest.mark.asyncio
async def test_deepseek_uses_model_summary_as_content(monkeypatch):
    from app.providers import deepseek

    calls: list[dict] = []
    data = {
        "results": [
            {
                "title": "DeepSeek V4",
                "url": "https://example.com/v4",
                "snippet": "短摘要",
                "summary": "给模型读的长摘要",
                "content": "",
                "site_icon": "https://example.com/favicon.ico",
                "date": "2026-06-01T00:00:00+00:00",
            },
            {"title": "无长摘要", "url": "https://example.com/b", "snippet": "只有短摘要", "summary": ""},
        ]
    }
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", _deepseek_client(data, calls))

    response = await deepseek.DeepSeekProvider().search(SearchRequest(query="DeepSeek V4"))

    assert calls[0]["json"] == {"query": "DeepSeek V4", "count": 10}
    assert response.provider == "deepseek"
    first, second = response.results
    assert first.description == "短摘要"
    assert first.content == "给模型读的长摘要"
    assert first.favicon == "https://example.com/favicon.ico"
    assert first.published_at == "2026-06-01T00:00:00+00:00"
    assert second.content == "只有短摘要"


@pytest.mark.asyncio
async def test_deepseek_rejects_image_search_so_route_can_fall_back():
    from app.providers.deepseek import DeepSeekProvider

    with pytest.raises(ValueError):
        await DeepSeekProvider().search(SearchRequest(query="cat", type=SearchType.IMAGE))
