import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

KEY = "only-on-the-server"


def settings(**values):
    return Settings(_env_file=None, gateway_url="http://gateway:8100", api_key=KEY, **values)


def upstream(request):
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-caller-scene"] == "playground"
    assert request.headers["x-caller-user"].startswith("visitor_")
    payload = json.loads(request.content)
    if payload["method"] == "initialize":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
    if payload["method"] == "notifications/initialized":
        return httpx.Response(202)
    name = payload["params"]["name"]
    if name == "search_web":
        data = {"ok": True, "results": [{"title": "Python", "url": "https://python.org", "snippet": "语言文档"}]}
    else:
        assert name == "read_page"
        assert payload["params"]["arguments"]["url"] == "https://python.org"
        data = {"ok": True, "title": "Python", "content": "# Python\n\n正文 **内容**"}
    reply = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": data}}
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"event: message\ndata: {json.dumps(reply)}\n\n",
    )


def test_search_then_read_uses_gateway_and_keeps_secret_server_side():
    with TestClient(create_app(settings(), transport=httpx.MockTransport(upstream))) as client:
        catalog = client.get("/api/catalog")
        assert catalog.status_code == 200
        assert len(catalog.json()["categories"]) == 8
        assert KEY not in catalog.text
        result = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert result.json()["ok"] is True
        hit = result.json()["result"]["results"][0]
        page = client.post("/api/run", json={"tool": "read_page", "arguments": {"url": hit["url"]}})
        assert page.json()["ok"] is True
        assert "<h1>Python</h1>" in page.json()["rendered_html"]
        assert KEY not in page.text


def test_unconfigured_page_works_and_run_is_explicitly_unavailable():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/catalog").json()["configured"] is False
        response = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_configured"


@pytest.mark.parametrize(
    "payload",
    [
        {"tool": "send_notification", "arguments": {}},
        {"tool": "search_web", "arguments": {"query": "Python", "count": 1000}},
        {"tool": "search_web", "arguments": {"query": "   "}},
        {"tool": "search_web", "arguments": {"query": "Python", "url": "http://internal"}},
        {"tool": "read_page", "arguments": {"url": "file:///etc/passwd"}},
        {"tool": "read_page", "arguments": {"url": "http://127.0.0.1"}},
        {"tool": "read_page", "arguments": {"url": "http://user:secret@example.com"}},
        {"tool": "generate_image", "arguments": {"prompt": "一座灯塔", "model": "unapproved-expensive-model"}},
    ],
)
def test_invalid_or_unapproved_calls_never_reach_gateway(payload):
    def reject(request):
        pytest.fail(f"不应调上游：{request.url.path}")

    with TestClient(create_app(settings(), transport=httpx.MockTransport(reject))) as client:
        response = client.post("/api/run", json=payload)
        assert response.status_code == 422


def test_origin_and_body_size_checked_before_upstream():
    with TestClient(create_app(settings())) as client:
        cross_site = client.post(
            "/api/run", headers={"origin": "https://another-site.example"}, json={"tool": "search_web", "arguments": {}}
        )
        assert cross_site.status_code == 403
        oversized = client.post("/api/run", content=b"x" * 20000, headers={"content-type": "application/json"})
        assert oversized.status_code == 413


def test_rate_limit_ignores_spoofed_identity_headers():
    with TestClient(create_app(settings(requests_per_minute=1), transport=httpx.MockTransport(upstream))) as client:
        body = {"tool": "search_web", "arguments": {"query": "Python"}}
        assert client.post("/api/run", json=body).status_code == 200
        limited = client.post("/api/run", json=body, headers={"x-forwarded-for": "9.9.9.9", "x-caller-user": "other"})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0


@pytest.mark.parametrize("kind", ["quota", "tool_error", "empty", "malformed", "timeout", "too_large"])
def test_upstream_failures_do_not_turn_into_success_or_leak_secrets(kind):
    def fail(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        if kind == "quota":
            return httpx.Response(
                429, json={"error": "daily_limit_exceeded", "message": KEY}, headers={"retry-after": "60"}
            )
        if kind == "tool_error":
            return httpx.Response(200, json={"result": {"isError": True, "content": [{"type": "text", "text": KEY}]}})
        if kind == "empty":
            return httpx.Response(200, json={"result": {}})
        if kind == "malformed":
            return httpx.Response(200, text="not json")
        if kind == "too_large":
            return httpx.Response(200, text="x" * (2 * 1024 * 1024 + 1))
        raise httpx.ReadTimeout("内部地址与密钥：" + KEY)

    with TestClient(create_app(settings(), transport=httpx.MockTransport(fail))) as client:
        response = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert response.status_code >= 400
        assert response.json()["ok"] is False
        assert KEY not in response.text
        if kind == "quota":
            assert response.headers["retry-after"] == "60"


def test_markdown_scripts_and_unsafe_links_are_inert():
    def malicious(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        text = "# 正文\n<script>alert(1)</script>\n[危险](javascript:alert(1))\n![跟踪](https://tracker.example/p.png)"
        return httpx.Response(200, json={"result": {"structuredContent": {"ok": True, "content": text}}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(malicious))) as client:
        response = client.post("/api/run", json={"tool": "read_page", "arguments": {"url": "https://python.org"}})
        rendered = response.json()["rendered_html"]
        assert "<script>" not in rendered
        assert 'href="javascript:' not in rendered
        assert "<img" not in rendered
        assert "<h1>正文</h1>" in rendered
