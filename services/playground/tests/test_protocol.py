import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.limits import Limiter, LimitError
from app.main import create_app
from app.settings import Settings
from tests.fake_gateway import tool_result
from tests.test_playground import settings, upstream


def test_stateful_session_is_initialized_used_and_closed_with_multiline_sse():
    methods = []

    def stateful(request):
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == "session-a"
            methods.append("DELETE")
            return httpx.Response(204)
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "session-a"}, json={"result": {"protocolVersion": "2025-03-26"}}
            )
        assert request.headers["mcp-session-id"] == "session-a"
        assert request.headers["mcp-protocol-version"] == "2025-03-26"
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        text = (
            ': heartbeat\n\ndata: {"method":"notifications/message"}\n\n'
            'event: message\ndata: {"id": 2,\n'
            'data: "result": {"structuredContent":{"ok":true,"results":[]}}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(stateful))) as client:
        response = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert response.status_code == 200
        assert response.json()["result"]["results"] == []
    assert methods == ["initialize", "notifications/initialized", "tools/call", "DELETE"]


def test_structured_failure_is_not_reported_as_success():
    def failure(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {
                        "ok": False,
                        "error": {
                            "code": "ambiguous_location",
                            "candidates": [{"label": "北京市朝阳区"}, {"label": "辽宁省朝阳市"}],
                        },
                    }
                }
            },
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(failure))) as client:
        response = client.post("/api/run", json={"tool": "get_weather", "arguments": {"location": "朝阳"}})
        assert response.status_code == 422
        assert response.json()["ok"] is False
        assert "多个同名" in response.json()["error"]["message"]
        assert len(response.json()["result"]["error"]["candidates"]) == 2


@pytest.mark.parametrize("content", [{"ok": True}, {"content": [None]}, {"content": "not-a-list"}])
def test_malformed_success_payload_is_not_an_empty_success(content):
    def malformed(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        result = content if "content" in content else {"structuredContent": content}
        return httpx.Response(200, json={"result": result})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(malformed))) as client:
        response = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert response.status_code == 502


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_weather", {"location": "杭州"}),
        ("place_search", {"city": "杭州", "query": "咖啡馆"}),
        ("route_compare", {"origin_city": "上海", "origin": "虹桥", "destination": "外滩"}),
        ("get_latest_news", {}),
        ("search_flights", {"origin": "北京", "destination": "上海", "departure_date": "2099-01-01"}),
        ("search_trains", {"origin": "杭州", "destination": "上海", "departure_date": "2099-01-01"}),
        ("resolve-library-id", {"libraryName": "FastAPI", "query": "lifespan"}),
        ("query-docs", {"libraryId": "/fastapi/fastapi", "query": "lifespan"}),
    ],
)
def test_other_tools_parse_their_mcp_result_contracts(name, args):
    def gateway(request):
        payload = json.loads(request.content)
        if payload["method"] != "tools/call":
            return upstream(request)
        data = tool_result(name, payload["params"]["arguments"])
        result = (
            {"structuredContent": data}
            if data is not None
            else {"content": [{"type": "text", "text": "# FastAPI\nLibrary ID: `/fastapi/fastapi`"}]}
        )
        return httpx.Response(200, json={"result": result})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(gateway))) as client:
        response = client.post("/api/run", json={"tool": name, "arguments": args})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        if name in {"resolve-library-id", "query-docs"}:
            assert "<h1>FastAPI</h1>" in response.json()["rendered_html"]


def test_generation_uses_rest_and_never_exposes_auth_headers():
    def generate(request):
        assert request.url.path == "/image/v1/generate"
        assert json.loads(request.content) == {"prompt": "灯塔", "quality": "standard", "aspect_ratio": "1:1"}
        return httpx.Response(
            200, json={"id": "image-a", "image_url": "https://example.com/image.png", "model": "test-only"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(generate))) as client:
        response = client.post("/api/run", json={"tool": "generate_image", "arguments": {"prompt": "灯塔"}})
        assert response.status_code == 200
        assert response.json()["result"]["image_url"] == "https://example.com/image.png"
        assert "authorization" not in response.text.lower()


def test_concurrency_released_after_failure_and_daily_image_limit_is_separate():
    async def scenario():
        limiter = Limiter(Settings(_env_file=None, max_concurrent=1, images_per_day=1))
        async with limiter.admit("a", image=True):
            with pytest.raises(LimitError) as held:
                async with limiter.admit("b"):
                    pass
            assert held.value.code == "busy"
        with pytest.raises(RuntimeError):
            async with limiter.admit("a"):
                raise RuntimeError("上游失败")
        async with limiter.admit("b"):
            pass
        with pytest.raises(LimitError) as limited:
            async with limiter.admit("a", image=True):
                pass
        assert limited.value.code == "daily_limit_exceeded"

    asyncio.run(scenario())


def test_configured_origin_and_browser_security_headers():
    with TestClient(
        create_app(settings(public_origin="https://tools.example"), transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/api/run",
            headers={"origin": "https://tools.example"},
            json={"tool": "search_web", "arguments": {"query": "Python"}},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "access-control-allow-origin" not in response.headers


def test_429_concurrency_is_not_reported_as_quota_exhaustion():
    def busy(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        return httpx.Response(429, json={"error": "concurrency_limited"}, headers={"retry-after": "3"})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(busy))) as client:
        response = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
