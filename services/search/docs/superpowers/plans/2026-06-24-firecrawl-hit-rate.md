# Firecrawl 命中率提升 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提升 Firecrawl 在中文联网问题上的首选提供方命中率，减少因为默认美国区域和过窄时效导致的 0 结果。

**Architecture:** 在 provider 层负责 Firecrawl 请求参数构造和同 provider 内部的宽松重试，route 层继续只负责缓存、provider 选择和跨 provider fallback。调用方未显式传 `region` 的中文查询默认使用更匹配的 `country=CN`；Firecrawl 中文查询首轮 `freshness=pw` 0 条时，先去掉 `tbs` 重试，再交给 route fallback；放宽时效拿到的结果不写缓存。

**Tech Stack:** FastAPI, Pydantic, httpx, pytest, ruff, Redis cache.

---

### Task 1: 中文查询默认区域推断

**Files:**
- Modify: `app/providers/firecrawl.py`
- Test: `tests/test_search.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_search.py` near existing Firecrawl parameter tests:

```python
async def test_firecrawl_defaults_chinese_queries_to_cn_region(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", _firecrawl_client({"data": {"web": []}}, calls))

    await firecrawl.FirecrawlProvider().search(SearchRequest(query="2026年中国结婚人数 是否创新低", count=5))

    assert calls[0]["json"]["country"] == "CN"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_search.py::test_firecrawl_defaults_chinese_queries_to_cn_region -q`

Expected: FAIL because current payload uses `country=US`.

- [ ] **Step 3: Implement minimal region inference**

In `app/providers/firecrawl.py`, add a helper that detects CJK characters and uses `CN` when the request still has the model default `region="us"`:

```python
def _country_for_request(self, request: SearchRequest) -> str | None:
    if not request.region:
        return None
    if request.region.lower() == "us" and self._contains_cjk(request.query):
        return "CN"
    return request.region.upper()

def _contains_cjk(self, value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)
```

Use `_country_for_request()` inside `_build_payload()` instead of directly uppercasing `request.region`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_search.py::test_firecrawl_defaults_chinese_queries_to_cn_region -q`

Expected: PASS.

### Task 2: Firecrawl 中文周内查询 0 结果时放宽时效重试

**Files:**
- Modify: `app/providers/firecrawl.py`
- Test: `tests/test_search.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_search.py`:

```python
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
                return httpx.Response(200, json={"data": {"web": []}}, request=httpx.Request("POST", url))
            return httpx.Response(
                200,
                json={
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
    assert calls[0]["json"]["tbs"] == "qdr:w"
    assert "tbs" not in calls[1]["json"]
    assert calls[1]["json"]["country"] == "CN"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_search.py::test_firecrawl_retries_without_freshness_when_first_response_is_empty -q`

Expected: FAIL because current provider only calls Firecrawl once.

- [ ] **Step 3: Implement minimal retry**

In `app/providers/firecrawl.py`, change `search()` so it builds the first payload, posts it, parses results, and when `results == []`, the query contains CJK, and `freshness == "pw"`, posts a second payload with `tbs` removed. Mark the response with `relaxed_freshness=True` only when the relaxed request returns non-empty results.

In `app/routes/search.py`, skip `set_cached()` when `result.relaxed_freshness` is true. In `app/cache.py`, bump the cache key version and include whether `region` was explicitly set, so old US-default cache entries and explicit-US queries do not collide with inferred-CN default queries.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_search.py::test_firecrawl_retries_without_freshness_when_first_response_is_empty -q`

Expected: PASS.

### Task 3: Regression and quality gates

**Files:**
- Verify only.

- [ ] **Step 1: Run Firecrawl/search tests**

Run: `uv run pytest tests/test_search.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run full tests**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Run architecture and lint checks**

Run:

```bash
uv run python scripts/check_architecture.py
uv run ruff check .
```

Expected: both commands pass.

- [ ] **Step 4: Commit**

Run:

```bash
git add app/providers/firecrawl.py tests/test_search.py docs/superpowers/plans/2026-06-24-firecrawl-hit-rate.md
git commit -m "fix: 提升 Firecrawl 中文搜索命中率" -m "Co-Authored-By: Codex <noreply@anthropic.com>"
```
