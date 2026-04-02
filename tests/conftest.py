from __future__ import annotations

from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from src.config import Settings
from src.main import create_app
from src.mcp.server import init_mcp, mcp
from src.services.cache import CacheService


# A minimal 1x1 red PNG for test fixtures
FAKE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)


def make_openrouter_response(image_b64: str = FAKE_PNG_B64) -> dict:
    """Build a fake OpenRouter chat completions response with an image.

    Uses the message.images[] format per OpenRouter docs.
    """
    return {
        "id": "gen-test-123",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Here is your generated image.",
                    "images": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                            },
                        }
                    ],
                }
            }
        ],
    }


class MockTransport(httpx.AsyncBaseTransport):
    """Mock httpx transport that returns canned OpenRouter responses."""

    def __init__(self, response_data: dict | None = None, status_code: int = 200):
        self._response_data = response_data or make_openrouter_response()
        self._status_code = status_code
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return httpx.Response(
            status_code=self._status_code,
            json=self._response_data,
        )


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    storage = tmp_path / "images"
    storage.mkdir()
    return storage


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings(tmp_storage: Path) -> Settings:
    return Settings(
        openrouter_api_key="sk-test-key",
        redis_url="redis://localhost:6379/0",
        storage_dir=str(tmp_storage),
        static_url_prefix="/static/images",
        default_model="google/gemini-2.5-flash-image",
        rate_limit_global=20,
        rate_limit_per_ip=5,
        rate_limit_window=60,
        cache_ttl_seconds=3600,
    )


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def app(settings: Settings, fake_redis, mock_transport: MockTransport) -> FastAPI:
    application = create_app(settings=settings)

    # Override lifespan-managed state for tests
    cache_service = CacheService(fake_redis, ttl=settings.cache_ttl_seconds)
    application.state.settings = settings
    application.state.redis = fake_redis
    application.state.cache = cache_service
    # Inject mock http client for upstream calls
    mock_http = httpx.AsyncClient(transport=mock_transport)
    application.state.http_client = mock_http
    # Init MCP with test dependencies
    application.state.mcp = mcp
    init_mcp(settings, cache_service, http_client=mock_http)

    return application


@pytest.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
