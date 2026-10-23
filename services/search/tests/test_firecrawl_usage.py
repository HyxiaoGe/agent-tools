import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx

REAL_ASYNC_CLIENT = httpx.AsyncClient


def _cache_wrapper(payload: dict, fetched_at: datetime) -> str:
    return json.dumps({"fetched_at": fetched_at.isoformat(), "payload": payload})


def _usage_client(response_data: dict, calls: list[dict], status_code: int = 200):
    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def get(self, url: str, headers: dict):
            calls.append({"url": url, "headers": headers, "timeout": self.timeout})
            return httpx.Response(status_code, json=response_data, request=httpx.Request("GET", url))

    return FakeAsyncClient


async def _get_usage():
    from app.main import app

    async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.get("/usage/firecrawl")


async def test_firecrawl_usage_endpoint_fetches_and_returns_credit_usage(monkeypatch):
    from app.models import ProviderRecordedUsage
    from app.providers import firecrawl, registry

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://firecrawl.example/api/")
    monkeypatch.setattr(
        registry,
        "get_recorded_provider_usage",
        AsyncMock(
            return_value=ProviderRecordedUsage(
                provider="firecrawl",
                available=True,
                credits_used=12,
                request_count=3,
                period_start="2025-01-01T00:00:00Z",
                period_end="2025-01-31T23:59:59Z",
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client(
            {
                "success": True,
                "data": {
                    "remainingCredits": 250,
                    "planCredits": 1000,
                    "billingPeriodStart": "2025-01-01T00:00:00Z",
                    "billingPeriodEnd": "2025-01-31T23:59:59Z",
                },
            },
            calls,
        ),
    )

    response = await _get_usage()

    assert response.status_code == 200
    assert response.json() == {
        "provider": "firecrawl",
        "available": True,
        "remaining_credits": 250,
        "plan_credits": 1000,
        "used_credits": 750,
        "usage_ratio": 0.75,
        "billing_period_start": "2025-01-01T00:00:00Z",
        "billing_period_end": "2025-01-31T23:59:59Z",
        "recorded_usage": {
            "provider": "firecrawl",
            "available": True,
            "credits_used": 12,
            "request_count": 3,
            "period_start": "2025-01-01T00:00:00Z",
            "period_end": "2025-01-31T23:59:59Z",
            "source": "search_response_credits_used",
            "daily": [],
        },
    }
    assert calls == [
        {
            "url": "https://firecrawl.example/api/v2/team/credit-usage",
            "headers": {"Authorization": "Bearer fc-test-key"},
            "timeout": 20,
        }
    ]


async def test_firecrawl_usage_endpoint_does_not_call_firecrawl_without_api_key(monkeypatch):
    from app.providers import firecrawl

    class ExplodingAsyncClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("缺少 Firecrawl API key 时不应创建 HTTP 客户端")

    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", ExplodingAsyncClient)

    response = await _get_usage()

    assert response.status_code == 200
    assert response.json() == {
        "provider": "firecrawl",
        "available": False,
        "remaining_credits": None,
        "plan_credits": None,
        "used_credits": None,
        "usage_ratio": None,
        "billing_period_start": None,
        "billing_period_end": None,
        "recorded_usage": None,
    }


async def test_firecrawl_usage_endpoint_leaves_used_and_ratio_null_when_remaining_exceeds_plan(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client(
            {
                "success": True,
                "data": {
                    "remainingCredits": 1200,
                    "planCredits": 1000,
                    "billingPeriodStart": "2025-01-01T00:00:00Z",
                    "billingPeriodEnd": "2025-01-31T23:59:59Z",
                },
            },
            calls,
        ),
    )

    response = await _get_usage()

    assert response.status_code == 200
    body = response.json()
    assert body["remaining_credits"] == 1200
    assert body["plan_credits"] == 1000
    assert body["used_credits"] is None
    assert body["usage_ratio"] is None


async def test_firecrawl_usage_endpoint_caches_provider_payload_but_refreshes_recorded_usage(monkeypatch):
    from app.models import ProviderRecordedUsage
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    calls: list[dict] = []
    recorded_calls = 0
    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    async def fake_recorded_usage(_provider: str, *, period_start: str | None, period_end: str | None):
        nonlocal recorded_calls
        recorded_calls += 1
        return ProviderRecordedUsage(
            provider="firecrawl",
            available=True,
            credits_used=recorded_calls * 10,
            request_count=recorded_calls,
            period_start=period_start,
            period_end=period_end,
        )

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry, "get_recorded_provider_usage", fake_recorded_usage, raising=False)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_CACHE_TTL", 300)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_STALE_TTL", 3600)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client(
            {
                "success": True,
                "data": {
                    "remainingCredits": 250,
                    "planCredits": 1000,
                    "billingPeriodStart": "2025-01-01T00:00:00Z",
                    "billingPeriodEnd": "2025-01-31T23:59:59Z",
                },
            },
            calls,
        ),
    )

    first = await _get_usage()
    second = await _get_usage()

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 1
    assert first.json()["recorded_usage"]["credits_used"] == 10
    assert second.json()["recorded_usage"]["credits_used"] == 20
    assert redis.values["firecrawl:usage:current:ttl"] == 3900


async def test_firecrawl_usage_endpoint_returns_stale_cache_and_refreshes_in_background(monkeypatch):
    from app.models import ProviderRecordedUsage
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    calls: list[dict] = []
    created_tasks = []
    redis = FakeRedis(
        {
            "firecrawl:usage:current": _cache_wrapper(
                {
                    "provider": "firecrawl",
                    "available": True,
                    "remaining_credits": 250,
                    "plan_credits": 1000,
                    "used_credits": 750,
                    "usage_ratio": 0.75,
                    "billing_period_start": "2026-06-01T00:00:00Z",
                    "billing_period_end": "2026-06-30T23:59:59Z",
                    "recorded_usage": None,
                },
                now - timedelta(seconds=301),
            )
        }
    )

    async def fake_get_redis():
        return redis

    def fake_create_task(coro):
        created_tasks.append(coro)
        return object()

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry, "_utc_now", lambda: now)
    monkeypatch.setattr(registry.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_CACHE_TTL", 300)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_STALE_TTL", 3600)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(
        registry,
        "get_recorded_provider_usage",
        AsyncMock(
            return_value=ProviderRecordedUsage(
                provider="firecrawl",
                available=True,
                credits_used=12,
                request_count=3,
                period_start="2026-06-01T00:00:00Z",
                period_end="2026-06-30T23:59:59Z",
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client(
            {
                "success": True,
                "data": {
                    "remainingCredits": 900,
                    "planCredits": 1000,
                    "billingPeriodStart": "2026-06-01T00:00:00Z",
                    "billingPeriodEnd": "2026-06-30T23:59:59Z",
                },
            },
            calls,
        ),
    )

    response = await _get_usage()

    assert response.status_code == 200
    body = response.json()
    assert body["remaining_credits"] == 250
    assert body["used_credits"] == 750
    assert body["recorded_usage"]["credits_used"] == 12
    assert calls == []
    assert len(created_tasks) == 1
    assert "firecrawl:usage:current:refresh_lock" not in redis.values

    await created_tasks[0]

    assert len(calls) == 1
    assert redis.values["firecrawl:usage:current:refresh_lock"] == "1"
    refreshed_payload = json.loads(redis.values["firecrawl:usage:current"])["payload"]
    assert refreshed_payload["remaining_credits"] == 900


async def test_firecrawl_usage_endpoint_schedules_lock_check_without_blocking_stale_response(monkeypatch):
    from app.models import ProviderRecordedUsage
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    created_tasks = []
    redis = FakeRedis(
        {
            "firecrawl:usage:current": _cache_wrapper(
                {
                    "provider": "firecrawl",
                    "available": True,
                    "remaining_credits": 250,
                    "plan_credits": 1000,
                    "used_credits": 750,
                    "usage_ratio": 0.75,
                    "billing_period_start": "2026-06-01T00:00:00Z",
                    "billing_period_end": "2026-06-30T23:59:59Z",
                    "recorded_usage": None,
                },
                now - timedelta(seconds=301),
            ),
            "firecrawl:usage:current:refresh_lock": "1",
        }
    )

    async def fake_get_redis():
        return redis

    def fake_create_task(coro):
        created_tasks.append(coro)
        return object()

    class ExplodingAsyncClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("refresh lock 存在时不应创建 Firecrawl 请求")

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry, "_utc_now", lambda: now)
    monkeypatch.setattr(registry.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_CACHE_TTL", 300)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_STALE_TTL", 3600)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", ExplodingAsyncClient)
    monkeypatch.setattr(
        registry,
        "get_recorded_provider_usage",
        AsyncMock(
            return_value=ProviderRecordedUsage(
                provider="firecrawl",
                available=True,
                credits_used=12,
                request_count=3,
                period_start="2026-06-01T00:00:00Z",
                period_end="2026-06-30T23:59:59Z",
            )
        ),
        raising=False,
    )

    response = await _get_usage()

    assert response.status_code == 200
    assert response.json()["remaining_credits"] == 250
    assert len(created_tasks) == 1

    await created_tasks[0]

    assert created_tasks


async def test_firecrawl_usage_endpoint_treats_cache_as_expired_after_fresh_plus_stale_window(monkeypatch):
    from app.models import ProviderRecordedUsage
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    calls: list[dict] = []
    redis = FakeRedis(
        {
            "firecrawl:usage:current": _cache_wrapper(
                {
                    "provider": "firecrawl",
                    "available": True,
                    "remaining_credits": 250,
                    "plan_credits": 1000,
                    "used_credits": 750,
                    "usage_ratio": 0.75,
                    "billing_period_start": "2026-06-01T00:00:00Z",
                    "billing_period_end": "2026-06-30T23:59:59Z",
                    "recorded_usage": None,
                },
                now - timedelta(seconds=3901),
            )
        }
    )

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry, "_utc_now", lambda: now)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_CACHE_TTL", 300)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_USAGE_STALE_TTL", 3600)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(
        registry,
        "get_recorded_provider_usage",
        AsyncMock(
            return_value=ProviderRecordedUsage(
                provider="firecrawl",
                available=True,
                credits_used=12,
                request_count=3,
                period_start="2026-06-01T00:00:00Z",
                period_end="2026-06-30T23:59:59Z",
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client(
            {
                "success": True,
                "data": {
                    "remainingCredits": 880,
                    "planCredits": 1000,
                    "billingPeriodStart": "2026-06-01T00:00:00Z",
                    "billingPeriodEnd": "2026-06-30T23:59:59Z",
                },
            },
            calls,
        ),
    )

    response = await _get_usage()

    assert response.status_code == 200
    assert response.json()["remaining_credits"] == 880
    assert len(calls) == 1


async def test_firecrawl_usage_endpoint_maps_firecrawl_failures_to_502_without_leaking_key(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-secret-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(
        firecrawl.httpx,
        "AsyncClient",
        _usage_client({"success": False, "error": "unauthorized fc-secret-key"}, calls, status_code=500),
    )

    response = await _get_usage()

    assert response.status_code == 502
    assert response.json() == {"detail": "Firecrawl usage request failed"}
    assert "fc-secret-key" not in response.text


async def test_firecrawl_historical_usage_endpoint_fetches_periods_and_masks_api_key(monkeypatch):
    from app.providers import firecrawl

    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def get(self, url: str, headers: dict, params: dict | None = None):
            calls.append({"url": url, "headers": headers, "params": params, "timeout": self.timeout})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "periods": [
                        {
                            "startDate": "2026-06-01T00:00:00Z",
                            "endDate": "2026-06-30T23:59:59Z",
                            "apiKey": "fc-1234567890abcdef",
                            "totalCredits": 42,
                        }
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    from app.main import app

    async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/usage/firecrawl/historical?by_api_key=true")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "firecrawl",
        "available": True,
        "by_api_key": True,
        "periods": [
            {
                "start_date": "2026-06-01T00:00:00Z",
                "end_date": "2026-06-30T23:59:59Z",
                "api_key": "fc-123...cdef",
                "total_credits": 42,
            }
        ],
    }
    assert calls == [
        {
            "url": "https://api.firecrawl.dev/v2/team/credit-usage/historical",
            "headers": {"Authorization": "Bearer fc-test-key"},
            "params": {"byApiKey": "true"},
            "timeout": 20,
        }
    ]
    assert "fc-1234567890abcdef" not in response.text


async def test_firecrawl_historical_usage_accepts_current_firecrawl_credits_used_shape(monkeypatch):
    from app.providers import firecrawl

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def get(self, url: str, headers: dict, params: dict | None = None):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "periods": [
                        {
                            "startDate": "2026-06-01T00:00:00.000Z",
                            "endDate": None,
                            "creditsUsed": 32,
                        }
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    from app.main import app

    async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/usage/firecrawl/historical")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "firecrawl",
        "available": True,
        "by_api_key": False,
        "periods": [
            {
                "start_date": "2026-06-01T00:00:00.000Z",
                "end_date": "",
                "api_key": None,
                "total_credits": 32,
            }
        ],
    }


async def test_firecrawl_historical_usage_endpoint_caches_by_api_key_variants_separately(monkeypatch):
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    calls: list[dict] = []
    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def get(self, url: str, headers: dict, params: dict | None = None):
            calls.append({"url": url, "headers": headers, "params": params, "timeout": self.timeout})
            total = 100 if params == {"byApiKey": "true"} else 50
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "periods": [
                        {
                            "startDate": "2026-06-01T00:00:00Z",
                            "endDate": "2026-06-30T23:59:59Z",
                            "totalCredits": total,
                        }
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL", 3600)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_HISTORICAL_USAGE_STALE_TTL", 86400)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    from app.main import app

    async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        first_default = await client.get("/usage/firecrawl/historical")
        second_default = await client.get("/usage/firecrawl/historical")
        first_by_key = await client.get("/usage/firecrawl/historical?by_api_key=true")
        second_by_key = await client.get("/usage/firecrawl/historical?by_api_key=true")

    assert first_default.status_code == 200
    assert second_default.status_code == 200
    assert first_by_key.status_code == 200
    assert second_by_key.status_code == 200
    assert len(calls) == 2
    assert first_default.json()["periods"][0]["total_credits"] == 50
    assert second_default.json()["periods"][0]["total_credits"] == 50
    assert first_by_key.json()["periods"][0]["total_credits"] == 100
    assert second_by_key.json()["periods"][0]["total_credits"] == 100
    assert redis.values["firecrawl:usage:historical:by_api_key:false:ttl"] == 90000
    assert redis.values["firecrawl:usage:historical:by_api_key:true:ttl"] == 90000


async def test_firecrawl_historical_usage_endpoint_returns_stale_cache_and_refreshes_in_background(monkeypatch):
    from app.providers import firecrawl, registry
    from tests.test_usage import FakeRedis

    now = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    calls: list[dict] = []
    created_tasks = []
    cache_key = "firecrawl:usage:historical:by_api_key:false"
    redis = FakeRedis(
        {
            cache_key: _cache_wrapper(
                {
                    "provider": "firecrawl",
                    "available": True,
                    "by_api_key": False,
                    "periods": [
                        {
                            "start_date": "2026-06-01T00:00:00Z",
                            "end_date": "2026-06-30T23:59:59Z",
                            "api_key": None,
                            "total_credits": 50,
                        }
                    ],
                },
                now - timedelta(seconds=3601),
            )
        }
    )

    async def fake_get_redis():
        return redis

    def fake_create_task(coro):
        created_tasks.append(coro)
        return object()

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        async def get(self, url: str, headers: dict, params: dict | None = None):
            calls.append({"url": url, "headers": headers, "params": params, "timeout": self.timeout})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "periods": [
                        {
                            "startDate": "2026-06-01T00:00:00Z",
                            "endDate": "2026-06-30T23:59:59Z",
                            "totalCredits": 99,
                        }
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(registry, "get_redis", fake_get_redis, raising=False)
    monkeypatch.setattr(registry, "_utc_now", lambda: now)
    monkeypatch.setattr(registry.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL", 3600)
    monkeypatch.setattr(registry.settings, "FIRECRAWL_HISTORICAL_USAGE_STALE_TTL", 86400)
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(firecrawl.settings, "FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", FakeAsyncClient)

    from app.main import app

    async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/usage/firecrawl/historical")

    assert response.status_code == 200
    assert response.json()["periods"][0]["total_credits"] == 50
    assert calls == []
    assert len(created_tasks) == 1
    assert f"{cache_key}:refresh_lock" not in redis.values

    await created_tasks[0]

    assert len(calls) == 1
    assert redis.values[f"{cache_key}:refresh_lock"] == "1"
    refreshed_payload = json.loads(redis.values[cache_key])["payload"]
    assert refreshed_payload["periods"][0]["total_credits"] == 99
