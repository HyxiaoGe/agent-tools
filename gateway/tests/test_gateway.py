from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import httpx
import pytest

from app.config import ProductConfig, Settings
from app.main import create_app
from app.usage_report import summarize

FUSION_KEY = "fusion-test-key"
VALIDATION_KEY = "validation-test-key"

pytestmark = pytest.mark.anyio


def respond(status: int, *, body: object = None, headers: dict[str, str] | None = None) -> httpx.Response:
    """真实上游的响应体是流式的；httpx.Response(json=...) 会预先读完响应体，不能用来模拟上游。"""
    content = b"" if body is None else json.dumps(body).encode()
    merged = {"content-type": "application/json", **(headers or {})} if body is not None else dict(headers or {})
    return httpx.Response(status, headers=merged, stream=httpx.ByteStream(content))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def product_keys(monkeypatch):
    monkeypatch.setenv("FUSION_SERVICE_KEY", FUSION_KEY)
    monkeypatch.setenv("VALIDATION_SERVICE_KEY", VALIDATION_KEY)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        products={
            "fusion": ProductConfig(service_key_env="FUSION_SERVICE_KEY"),
            "validation": ProductConfig(service_key_env="VALIDATION_SERVICE_KEY"),
        },
        usage_db_path=str(tmp_path / "usage.sqlite3"),
    )


class Upstreams:
    """记录每个服务收到的请求，按服务返回预设响应或抛出预设异常。"""

    def __init__(self):
        self.requests: dict[str, list[httpx.Request]] = {"image": [], "search": [], "reader": []}
        self.handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def transport(self, service: str) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests[service].append(request)
            handler = self.handlers.get(service)
            if handler is None:
                return respond(200, body={"service": service})
            return handler(request)

        return httpx.MockTransport(handle)


@pytest.fixture
def upstreams() -> Upstreams:
    return Upstreams()


@pytest.fixture
async def client(settings, upstreams):
    app = create_app(settings, {name: upstreams.transport(name) for name in ("image", "search", "reader")})
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            yield http


def usage_rows(settings: Settings) -> list[dict]:
    conn = sqlite3.connect(settings.usage_db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("SELECT * FROM requests ORDER BY id")]
    conn.close()
    return rows


def auth(key: str = FUSION_KEY, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


async def test_health_needs_no_key(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": FUSION_KEY}])
async def test_rejects_requests_without_a_product_key(client, upstreams, headers):
    response = await client.post("/search/search", json={"query": "x"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert upstreams.requests["search"] == []


async def test_unknown_service_is_not_found(client):
    response = await client.get("/video/anything", headers=auth())
    assert response.status_code == 404


async def test_search_request_carries_caller_but_not_the_key(client, upstreams, settings):
    upstreams.handlers["search"] = lambda request: respond(200, body={"results": [1, 2]})

    response = await client.post(
        "/search/search?fresh=1",
        json={"query": "天气"},
        headers=auth(
            **{
                "X-Caller-User": "user-1",
                "X-Caller-Scene": "chat_search",
                "X-Request-ID": "run-42",
                "X-Image-User": "spoofed",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json() == {"results": [1, 2]}
    (sent,) = upstreams.requests["search"]
    assert sent.url.path == "/search"
    assert sent.url.query == b"fresh=1"
    assert json.loads(sent.content) == {"query": "天气"}
    assert "authorization" not in sent.headers
    assert "x-image-user" not in sent.headers
    assert sent.headers["x-caller-app"] == "fusion"
    assert sent.headers["x-caller-user"] == "user-1"
    assert sent.headers["x-caller-scene"] == "chat_search"
    assert sent.headers["x-request-id"] == "run-42"
    (row,) = usage_rows(settings)
    assert row["app_id"] == "fusion"
    assert row["user_id"] == "user-1"
    assert row["scene"] == "chat_search"
    assert row["request_id"] == "run-42"
    assert row["service"] == "search"
    assert row["path"] == "/search"
    assert row["status"] == 200
    assert row["outcome"] == "ok"


async def test_caller_cannot_assert_another_product(client, upstreams):
    await client.get("/reader/read", headers=auth(VALIDATION_KEY, **{"X-Caller-App": "fusion"}))
    (sent,) = upstreams.requests["reader"]
    assert sent.headers["x-caller-app"] == "validation"


async def test_image_receives_its_own_identity_headers(client, upstreams):
    await client.post(
        "/image/mcp",
        content=b'{"jsonrpc":"2.0"}',
        headers=auth(**{"X-Caller-User": "user-1", "X-Caller-Scene": "image_edit"}),
    )
    (sent,) = upstreams.requests["image"]
    assert sent.url.path == "/mcp"
    # image-service 仍按同一把产品密钥认定产品，并从自己的头读取用户与场景。
    assert sent.headers["authorization"] == f"Bearer {FUSION_KEY}"
    assert sent.headers["x-image-user"] == "user-1"
    assert sent.headers["x-image-scene"] == "image_edit"


async def test_image_without_declared_user_sends_no_user_header(client, upstreams):
    await client.post("/image/mcp", content=b"{}", headers=auth(**{"X-Image-User": "spoofed"}))
    (sent,) = upstreams.requests["image"]
    assert "x-image-user" not in sent.headers


@pytest.mark.parametrize(
    "header",
    [{"X-Caller-User": "has space"}, {"X-Caller-Scene": "Chat-Image"}, {"X-Request-ID": "x" * 129}],
)
async def test_malformed_caller_headers_are_rejected(client, upstreams, header):
    response = await client.get("/search/health", headers=auth(**header))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_caller_header"
    assert upstreams.requests["search"] == []


async def test_upstream_status_and_streamed_body_pass_through(client, upstreams):
    def sse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "mcp-session-id": "s-1"},
            stream=httpx.ByteStream(b"event: message\ndata: {}\n\n"),
        )

    upstreams.handlers["image"] = sse
    response = await client.post("/image/mcp", content=b"{}", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["mcp-session-id"] == "s-1"
    assert response.content == b"event: message\ndata: {}\n\n"


async def test_upstream_errors_are_returned_as_is(client, upstreams, settings):
    upstreams.handlers["reader"] = lambda request: respond(422, body={"error": "page_not_found"})
    response = await client.post("/reader/read", json={}, headers=auth())
    assert response.status_code == 422
    assert response.json() == {"error": "page_not_found"}
    (row,) = usage_rows(settings)
    assert (row["status"], row["outcome"]) == (422, "ok")


@pytest.mark.parametrize(
    ("error", "status", "code", "outcome"),
    [
        (httpx.PoolTimeout("full"), 503, "upstream_busy", "busy"),
        (httpx.ReadTimeout("slow"), 504, "upstream_timeout", "timeout"),
        (httpx.ConnectError("down"), 502, "upstream_unreachable", "unreachable"),
    ],
)
async def test_gateway_failures_are_explicit_and_recorded(client, upstreams, settings, error, status, code, outcome):
    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    upstreams.handlers["search"] = fail
    response = await client.post("/search/search", json={}, headers=auth())
    assert response.status_code == status
    assert response.json()["error"] == code
    (row,) = usage_rows(settings)
    assert (row["status"], row["outcome"]) == (status, outcome)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("http://host.docker.internal:8090/mcp/", "/image/mcp/"),
        ("/mcp/", "/image/mcp/"),
        ("https://example.com/x", "https://example.com/x"),
    ],
)
async def test_redirects_stay_behind_the_gateway(client, upstreams, location, expected):
    upstreams.handlers["image"] = lambda request: respond(307, headers={"location": location})
    response = await client.post("/image/mcp", content=b"{}", headers=auth())
    assert response.status_code == 307
    assert response.headers["location"] == expected


async def test_oversized_body_is_rejected(settings, upstreams):
    settings.max_request_body_bytes = 10
    app = create_app(settings, {"search": upstreams.transport("search")})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as http:
            response = await http.post("/search/search", content=b"x" * 11, headers=auth())
    assert response.status_code == 413
    assert upstreams.requests["search"] == []


def test_products_must_not_share_a_key():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            products={
                "fusion": ProductConfig(service_key_env="SAME_KEY"),
                "validation": ProductConfig(service_key_env="SAME_KEY"),
            },
        )


async def test_product_without_a_configured_key_cannot_match(client, monkeypatch):
    monkeypatch.setenv("VALIDATION_SERVICE_KEY", "")
    response = await client.get("/search/health", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


async def test_usage_report_groups_by_product_service_and_scene(client, settings):
    await client.post("/search/search", json={}, headers=auth(**{"X-Caller-User": "a", "X-Caller-Scene": "chat"}))
    await client.post("/search/search", json={}, headers=auth(**{"X-Caller-User": "b", "X-Caller-Scene": "chat"}))
    await client.post("/reader/read", json={}, headers=auth(VALIDATION_KEY))

    report = summarize(settings.usage_db_path, app_id="fusion", days=1)

    assert report["breakdown"] == [
        {
            "app_id": "fusion",
            "service": "search",
            "scene": "chat",
            "requests": 2,
            "users": 2,
            "succeeded": 2,
            "failed": 0,
            "avg_duration_ms": report["breakdown"][0]["avg_duration_ms"],
        }
    ]
