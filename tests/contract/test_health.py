"""Contract tests for GET /v1/health."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.asyncio
async def test_health_healthy(client: httpx.AsyncClient):
    """T034: Returns healthy status when all deps OK."""
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert "checks" in data
    assert data["checks"]["redis"] == "ok"
    assert data["checks"]["storage"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded_redis(client: httpx.AsyncClient, app):
    """T035: Returns degraded when Redis is unavailable."""
    # Make redis ping fail
    original_ping = app.state.cache.ping
    app.state.cache.ping = AsyncMock(return_value=False)

    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert "error" in data["checks"]["redis"] or data["checks"]["redis"] != "ok"

    # Restore
    app.state.cache.ping = original_ping
