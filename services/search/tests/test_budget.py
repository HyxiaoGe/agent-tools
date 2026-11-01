import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import BackgroundTasks

from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType


class FakeRedis:
    def __init__(self):
        self.values: dict[str, int | str] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = int(self.values.get(key, 0)) - 1
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]


@pytest.fixture
def budget_env(monkeypatch):
    from app import budget

    redis = FakeRedis()
    sent: list[str] = []

    async def fake_get_redis():
        return redis

    async def fake_send(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)
    monkeypatch.setattr(budget, "send_feishu_text", fake_send)
    monkeypatch.setattr(budget, "budget_day", lambda now=None: "2026-11-01")
    monkeypatch.setattr(budget.settings, "BUDGET_ALERT_RATIO", 0.8)
    return budget, redis, sent


async def _drain_alerts(budget) -> None:
    while budget._pending_alerts:
        await asyncio.gather(*list(budget._pending_alerts))


def _response(provider: str, count: int = 2) -> SearchResponse:
    return SearchResponse(
        query="q",
        type=SearchType.WEB,
        provider=provider,
        results=[SearchResultItem(title=f"t{i}", url=f"https://e.com/{i}", description="d") for i in range(count)],
    )


class FakeUpstream:
    def __init__(self, provider: str):
        self.provider = provider
        self.api_key = f"{provider}-key"
        self.calls = 0

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.calls += 1
        return _response(self.provider)


def test_parse_daily_limits_accepts_spaces_case_and_zero():
    from app.config import parse_daily_limits

    assert parse_daily_limits(" brave=100, DeepSeek = 0 ,,") == {"brave": 100, "deepseek": 0}
    assert parse_daily_limits("") == {}


@pytest.mark.parametrize("raw", ["brave", "brave=abc", "brave=-1", "=5"])
def test_parse_daily_limits_rejects_bad_entries(raw):
    from app.config import parse_daily_limits

    with pytest.raises(ValueError):
        parse_daily_limits(raw)


def test_settings_refuse_to_start_with_bad_limits():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(PROVIDER_DAILY_LIMITS="brave=abc")


def test_budget_day_rolls_over_at_beijing_midnight():
    from app.budget import budget_day

    assert budget_day(datetime(2026, 10, 31, 15, 59, tzinfo=UTC)) == "2026-10-31"
    assert budget_day(datetime(2026, 10, 31, 16, 0, tzinfo=UTC)) == "2026-11-01"


async def test_reserve_counts_calls_and_rejects_over_limit_without_counting(monkeypatch, budget_env):
    budget, redis, _sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=3")

    for _ in range(3):
        await budget.reserve_provider_call("brave")
    with pytest.raises(budget.ProviderBudgetExceeded) as exc_info:
        await budget.reserve_provider_call("brave")
    with pytest.raises(budget.ProviderBudgetExceeded):
        await budget.reserve_provider_call("brave")
    await _drain_alerts(budget)

    assert redis.values["budget:brave:calls:day:2026-11-01"] == 3
    assert redis.values["budget:brave:rejected:day:2026-11-01"] == 2
    assert redis.ttls["budget:brave:calls:day:2026-11-01"] == 3 * 86400
    assert redis.ttls["budget:brave:rejected:day:2026-11-01"] == 3 * 86400
    assert exc_info.value.provider == "brave"
    assert exc_info.value.limit == 3


async def test_unlisted_provider_is_unlimited_and_skips_redis(monkeypatch):
    from app import budget

    async def boom():
        raise AssertionError("不应访问 Redis")

    monkeypatch.setattr(budget, "get_redis", boom)
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=1")

    await budget.reserve_provider_call("tavily")


async def test_zero_limit_blocks_every_call_and_only_sends_exhausted_alert(monkeypatch, budget_env):
    budget, _redis, sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=0")

    with pytest.raises(budget.ProviderBudgetExceeded):
        await budget.reserve_provider_call("brave")
    await _drain_alerts(budget)

    assert len(sent) == 1
    assert "上限 0 次" in sent[0]


async def test_redis_failure_lets_call_through(monkeypatch):
    from app import budget

    async def broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(budget, "get_redis", broken_redis)
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=1")

    await budget.reserve_provider_call("brave")
    await budget.reserve_provider_call("brave")


async def test_warn_and_exhausted_alerts_fire_once_per_day(monkeypatch, budget_env):
    budget, _redis, sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=5")

    for _ in range(5):
        await budget.reserve_provider_call("brave")
    for _ in range(3):
        with pytest.raises(budget.ProviderBudgetExceeded):
            await budget.reserve_provider_call("brave")
    await _drain_alerts(budget)

    assert len(sent) == 2
    assert "4/5" in sent[0]
    assert "上限 5 次" in sent[1]


async def test_guarded_provider_skips_upstream_once_exhausted(monkeypatch, budget_env):
    budget, _redis, _sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=1")
    upstream = FakeUpstream("brave")
    guarded = budget.BudgetGuardedProvider("brave", upstream)

    await guarded.search(SearchRequest(query="q"))
    with pytest.raises(budget.ProviderBudgetExceeded):
        await guarded.search(SearchRequest(query="q"))
    await _drain_alerts(budget)

    assert upstream.calls == 1
    assert guarded.api_key == "brave-key"


def _http_request():
    from starlette.requests import Request

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


def _patch_route(monkeypatch, primary, fallback_name, fallback):
    from app.routes import search as search_route

    async def no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(search_route, "get_cached", no_cache)
    monkeypatch.setattr(search_route, "set_cached", no_cache)
    monkeypatch.setattr(search_route, "get_provider", lambda _name=None: primary)
    monkeypatch.setattr(search_route, "get_fallback_providers", lambda _primary: [(fallback_name, fallback)])
    return search_route


async def test_route_uses_fallback_when_primary_budget_exhausted(monkeypatch, budget_env):
    budget, _redis, _sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "deepseek=0,brave=10")
    deepseek, brave = FakeUpstream("deepseek"), FakeUpstream("brave")
    search_route = _patch_route(
        monkeypatch,
        budget.BudgetGuardedProvider("deepseek", deepseek),
        "brave",
        budget.BudgetGuardedProvider("brave", brave),
    )

    response = await search_route.search.__wrapped__(
        _http_request(), SearchRequest(query="q", provider="deepseek", count=2), BackgroundTasks()
    )
    await _drain_alerts(budget)

    assert response.result_provider == "brave"
    assert response.fallback_used is True
    assert deepseek.calls == 0
    assert brave.calls == 1


async def test_route_returns_503_when_primary_and_fallback_are_exhausted(monkeypatch, budget_env):
    budget, _redis, _sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "deepseek=0,brave=0")
    deepseek, brave = FakeUpstream("deepseek"), FakeUpstream("brave")
    search_route = _patch_route(
        monkeypatch,
        budget.BudgetGuardedProvider("deepseek", deepseek),
        "brave",
        budget.BudgetGuardedProvider("brave", brave),
    )

    with pytest.raises(budget.ProviderBudgetExceeded) as exc_info:
        await search_route.search.__wrapped__(
            _http_request(), SearchRequest(query="q", provider="deepseek", count=2), BackgroundTasks()
        )
    await _drain_alerts(budget)

    from app.main import _budget_exceeded_handler

    resp = await _budget_exceeded_handler(_http_request(), exc_info.value)
    assert resp.status_code == 503
    assert json.loads(resp.body)["provider"] == "deepseek"
    assert deepseek.calls == 0
    assert brave.calls == 0


async def test_send_feishu_text_signs_payload(monkeypatch):
    from app import budget

    posted: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url: str, json: dict):
            posted.append({"url": url, "json": json, "timeout": self.timeout})
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(budget.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(budget.settings, "BUDGET_ALERT_WEBHOOK", "https://open.feishu.cn/hook/x")
    monkeypatch.setattr(budget.settings, "BUDGET_ALERT_SIGN_SECRET", "s3cret")

    await budget.send_feishu_text("hello")

    payload = posted[0]["json"]
    expected = base64.b64encode(
        hmac.new(f"{payload['timestamp']}\ns3cret".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert payload["content"] == {"text": "hello"}
    assert payload["sign"] == expected
    assert posted[0]["timeout"] == 5


async def test_send_feishu_text_is_noop_without_webhook(monkeypatch):
    from app import budget

    def no_client(*_args, **_kwargs):
        raise AssertionError("未配置 webhook 时不应发请求")

    monkeypatch.setattr(budget.httpx, "AsyncClient", no_client)
    monkeypatch.setattr(budget.settings, "BUDGET_ALERT_WEBHOOK", "")

    await budget.send_feishu_text("hello")


async def test_budget_status_reports_used_and_rejected(monkeypatch, budget_env):
    budget, redis, _sent = budget_env
    monkeypatch.setattr(budget.settings, "PROVIDER_DAILY_LIMITS", "brave=2,deepseek=10")
    redis.values["budget:brave:calls:day:2026-11-01"] = 2
    redis.values["budget:brave:rejected:day:2026-11-01"] = 7

    status = {s.provider: s for s in await budget.get_budget_status()}

    assert status["brave"].used == 2
    assert status["brave"].rejected == 7
    assert status["brave"].limit == 2
    assert status["deepseek"].used == 0
    assert status["deepseek"].day == "2026-11-01"
