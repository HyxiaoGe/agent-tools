"""PROVIDER_DAILY_LIMITS：按上游的每日额度，用满时生图走备用链、参考图搜索跳过。"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import SecretStr

from src.config import Settings, parse_provider_limits
from src.services import generator, quota
from tests.conftest import make_gemini_response
from tests.contract.test_reference_search import Web, public_dns  # noqa: F401  (autouse DNS stub)

GEMINI = "gemini-3-pro-image-preview"  # default model in tests (provider google)
SEEDREAM = "doubao-seedream-4-5"  # provider volcano


async def post(client, prompt: str = "a poster", **body):
    return await client.post("/v1/generate", json={"prompt": prompt, **body})


@pytest.fixture
def gemini_ok(routing):
    routing.gemini = (200, make_gemini_response())
    return routing


async def usage(app, provider: str) -> tuple[int, int]:
    summary = await quota.daily_summary(app.state.redis, app.state.settings, quota.beijing_today())
    row = next(p for p in summary["providers"] if p["provider"] == provider)
    return row["used"], row["rejected"]


@pytest.mark.asyncio
async def test_primary_over_quota_falls_back_without_calling_it(client, app, gemini_ok):
    app.state.settings.provider_daily_limits = "google=1"

    first = (await post(client, "poster one", fallback_model=SEEDREAM)).json()
    second = (await post(client, "poster two", fallback_model=SEEDREAM)).json()

    assert first["model"] == GEMINI
    assert second["model"] == SEEDREAM and second["fallback_used"] is True
    assert gemini_ok.calls == {"gemini": 1, "ark": 1}
    assert await usage(app, "google") == (1, 1)
    # 没设上限的上游也计数
    assert await usage(app, "volcano") == (1, 0)


@pytest.mark.asyncio
async def test_every_model_over_quota_returns_quota_exceeded(client, app, gemini_ok):
    app.state.settings.provider_daily_limits = "google=0,volcano=0"

    resp = await post(client, fallback_model=SEEDREAM)

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "quota_exceeded"
    assert body["retryable"] is False
    assert 1 <= int(resp.headers["Retry-After"]) <= 86400
    assert gemini_ok.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_cache_hits_do_not_use_quota(client, app, gemini_ok):
    app.state.settings.provider_daily_limits = "google=1"

    first = await post(client, "same prompt")
    again = await post(client, "same prompt")

    assert first.status_code == again.status_code == 200
    assert again.json()["cached"] is True
    assert gemini_ok.calls["gemini"] == 1
    assert await usage(app, "google") == (1, 0)


@pytest.mark.asyncio
async def test_quota_counter_failure_does_not_reach_the_upstream(client, gemini_ok, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise quota.QuotaUnavailable

    monkeypatch.setattr(generator, "reserve", broken)

    resp = await post(client, fallback_model=SEEDREAM)

    assert resp.status_code == 503
    assert resp.json()["error"] == "admission_unavailable"
    assert gemini_ok.calls == {"gemini": 0, "ark": 0}


@pytest.mark.asyncio
async def test_reserve_fails_closed_when_redis_errors(settings, caplog):
    class BrokenRedis:
        def pipeline(self, **_kwargs):
            raise ConnectionError("redis://secret-host:6379 refused")

    with pytest.raises(quota.QuotaUnavailable):
        await quota.reserve(BrokenRedis(), settings, "google")
    assert "provider_quota_unavailable" in caplog.text
    assert "secret-host" not in caplog.text


@pytest.mark.asyncio
async def test_tavily_over_quota_skips_reference_search_but_still_generates(client, app):
    app.state.settings.tavily_api_keys = "key-a"
    web = Web()
    app.state.http_client = httpx.AsyncClient(transport=web)
    from src.mcp.server import init_mcp

    init_mcp(app.state.settings, app.state.cache, http_client=app.state.http_client)
    app.state.settings.provider_daily_limits = "tavily=0"

    resp = await post(client, "iPhone 18 Pro launch poster", reference_subject="iPhone 18 Pro")

    assert resp.status_code == 200, resp.text
    assert resp.json()["reference_search"]["status"] == "search_failed"
    assert web.tavily_calls() == []
    assert len(web.generations()) == 1
    assert await usage(app, "tavily") == (0, 1)


@pytest.mark.asyncio
async def test_quota_endpoint_is_public_and_reports_limits(app, client, gemini_ok):
    app.state.settings.provider_daily_limits = "google=1,tavily=20"
    await post(client, "poster one")
    await post(client, "poster two")  # 用满，没有备用 → quota_exceeded

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anonymous:
        resp = await anonymous.get("/v1/quota")
        bad = await anonymous.get("/v1/quota", params={"day": "11/01"})
        past = await anonymous.get("/v1/quota", params={"day": "2026-01-01"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True and data["day"] == quota.beijing_today().isoformat()
    assert data["providers"] == [
        {"provider": "volcano", "limit": None, "used": 0, "rejected": 0},
        {"provider": "google", "limit": 1, "used": 1, "rejected": 1},
        {"provider": "alibaba", "limit": None, "used": 0, "rejected": 0},
        {"provider": "tavily", "limit": 20, "used": 0, "rejected": 0},
    ]
    assert bad.status_code == 422
    assert all(p["used"] == 0 for p in past.json()["providers"])


@pytest.mark.asyncio
async def test_alerts_once_at_ratio_and_once_when_exhausted(settings, fake_redis, monkeypatch):
    posted: list[dict] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url: str, json: dict):
            posted.append({"url": url, "json": json})
            return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(quota, "_feishu_client", FakeClient)
    settings.provider_daily_limits = "google=5"
    settings.quota_alert_webhook = SecretStr("https://open.feishu.cn/open-apis/bot/v2/hook/patrol-token")
    settings.quota_alert_sign_secret = SecretStr("patrol-secret")

    allowed = []
    for _ in range(8):
        try:
            await quota.reserve(fake_redis, settings, "google")
            allowed.append(True)
        except quota.QuotaExceeded:
            allowed.append(False)
    await asyncio.gather(*quota._alert_tasks)

    assert allowed == [True] * 5 + [False] * 3
    texts = [p["json"]["content"]["text"] for p in posted]
    assert len(texts) == 2
    assert "google 今日上游已调用 4 次" in texts[0] and "80%" in texts[0]
    assert "google 今日上游调用已达每日上限 5 次" in texts[1]
    assert all(p["json"]["sign"] for p in posted)
    assert "patrol-token" not in repr(settings)


@pytest.mark.asyncio
async def test_alert_failure_logs_never_contain_the_webhook(monkeypatch, caplog):
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/patrol-token"

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url: str, json: dict):
            raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(quota, "_feishu_client", FailingClient)

    await quota._send_feishu(webhook, "", "hi")

    assert "quota_alert_failed" in caplog.text and "ConnectError" in caplog.text
    assert "patrol-token" not in caplog.text


def test_counters_follow_beijing_days():
    assert quota.beijing_today(datetime(2026, 11, 1, 15, 59, tzinfo=UTC)) == date(2026, 11, 1)
    assert quota.beijing_today(datetime(2026, 11, 1, 16, 0, tzinfo=UTC)) == date(2026, 11, 2)
    assert quota.seconds_until_reset(datetime(2026, 11, 1, 15, 59, tzinfo=UTC)) == 60


def test_provider_limits_are_validated():
    assert parse_provider_limits(" Volcano=100, tavily=0 ,") == {"volcano": 100, "tavily": 0}
    for raw in ("volcano", "volcano=-1", "volcano=lots", "openai=5"):
        with pytest.raises(ValueError):
            parse_provider_limits(raw)
    with pytest.raises(ValueError, match="unknown provider"):
        Settings(_env_file=None, provider_daily_limits="seedream=5")
