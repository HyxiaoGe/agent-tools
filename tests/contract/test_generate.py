"""Contract tests for POST /v1/generate."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import MockTransport


@pytest.mark.asyncio
async def test_generate_success(client: httpx.AsyncClient):
    """T014: Success path — returns image URL matching schema."""
    resp = await client.post("/v1/generate", json={"prompt": "a sunset over mountains"})

    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert "image_url" in data
    assert data["model"] == "gemini-3-pro-image-preview"
    assert data["cached"] is False
    assert "created_at" in data
    # Image should be accessible via static files
    img_resp = await client.get(data["image_url"].replace("http://test", ""))
    assert img_resp.status_code == 200


@pytest.mark.asyncio
async def test_generate_missing_prompt(client: httpx.AsyncClient):
    """T015: Missing prompt → 422 validation error."""
    resp = await client.post("/v1/generate", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_invalid_model(client: httpx.AsyncClient):
    """T015: Invalid model → 400."""
    resp = await client.post(
        "/v1/generate",
        json={"prompt": "test", "model": "nonexistent/model"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "model_not_found"


@pytest.mark.asyncio
async def test_generate_invalid_aspect_ratio(client: httpx.AsyncClient):
    """T015: Invalid aspect_ratio → 422."""
    resp = await client.post(
        "/v1/generate",
        json={"prompt": "test", "aspect_ratio": "invalid"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generate_upstream_error(
    client: httpx.AsyncClient,
    app,
):
    """T016: Upstream 500 → 502 with ErrorResponse."""
    # Replace the http_client with one that returns errors
    error_transport = MockTransport(
        response_data={"error": {"message": "upstream error"}},
        status_code=500,
    )
    app.state.http_client = httpx.AsyncClient(transport=error_transport)

    resp = await client.post("/v1/generate", json={"prompt": "a sunset"})

    assert resp.status_code == 502
    data = resp.json()
    assert data["error"] == "generation_failed"
    assert "message" in data
