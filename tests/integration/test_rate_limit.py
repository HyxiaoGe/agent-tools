"""Integration tests for rate limiting."""

from __future__ import annotations

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from src.config import Settings
from src.main import create_app
from src.mcp.server import init_mcp, mcp
from src.services.cache import CacheService
from tests.conftest import MockTransport


@pytest.fixture
def rate_limit_settings(tmp_path) -> Settings:
    storage = tmp_path / "images"
    storage.mkdir()
    return Settings(
        google_api_key="test-api-key",
        redis_url="redis://localhost:6379/0",
        storage_dir=str(storage),
        static_url_prefix="/static/images",
        default_model="gemini-3-pro-image-preview",
        rate_limit_global=5,  # Low limits for testing
        rate_limit_per_ip=3,
        rate_limit_window=60,
        cache_ttl_seconds=3600,
    )


@pytest.fixture
def rate_limit_app(rate_limit_settings: Settings) -> FastAPI:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = MockTransport()
    application = create_app(settings=rate_limit_settings)
    cache_service = CacheService(redis, ttl=rate_limit_settings.cache_ttl_seconds)
    mock_http = httpx.AsyncClient(transport=transport)
    application.state.settings = rate_limit_settings
    application.state.redis = redis
    application.state.cache = cache_service
    application.state.http_client = mock_http
    application.state.mcp = mcp
    init_mcp(rate_limit_settings, cache_service, http_client=mock_http)
    return application


@pytest.fixture
async def rate_limit_client(rate_limit_app: FastAPI) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rate_limit_app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_per_ip_rate_limit(rate_limit_client: httpx.AsyncClient):
    """T030: Per-IP rate limit — 4th request from same IP returns 429."""
    # Each request needs a unique prompt to avoid cache hits
    for i in range(3):
        resp = await rate_limit_client.post(
            "/v1/generate",
            json={"prompt": f"unique prompt {i}"},
        )
        assert resp.status_code == 200, f"Request {i + 1} should succeed"

    # 4th request should be rate limited (per_ip limit is 3)
    resp = await rate_limit_client.post(
        "/v1/generate",
        json={"prompt": "unique prompt overflow"},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert data["error"] == "rate_limited"
    assert "retry_after" in data


@pytest.mark.asyncio
async def test_global_rate_limit(rate_limit_client: httpx.AsyncClient, rate_limit_app: FastAPI):
    """T031: Global rate limit — 6th request returns 429."""
    # Override per-IP to be higher so global triggers first
    rate_limit_app.state.settings.rate_limit_per_ip = 100

    for i in range(5):
        resp = await rate_limit_client.post(
            "/v1/generate",
            json={"prompt": f"global test prompt {i}"},
        )
        assert resp.status_code == 200, f"Request {i + 1} should succeed"

    # 6th request should hit global limit (global limit is 5)
    resp = await rate_limit_client.post(
        "/v1/generate",
        json={"prompt": "global overflow prompt"},
    )
    assert resp.status_code == 429
