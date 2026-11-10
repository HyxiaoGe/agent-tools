import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_playground import settings, upstream


@pytest.mark.parametrize("encoding", ["wrapped", "structured", "text"])
def test_trends_accepts_real_fastmcp_response_and_requests_source_links(encoding):
    data = {
        "success": True,
        "summary": {"total": 1, "returned": 1},
        "data": [
            {
                "title": "热榜标题",
                "platform": "weibo",
                "platform_name": "微博",
                "rank": 1,
                "timestamp": "2026-11-10 01:30:00",
                "url": "https://example.com/news",
            }
        ],
    }

    def gateway(request):
        payload = json.loads(request.content)
        if payload["method"] != "tools/call":
            return upstream(request)
        assert payload["params"]["name"] == "get_latest_news"
        assert payload["params"]["arguments"] == {"limit": 10, "include_url": True}
        result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False}
        if encoding != "text":
            result["structuredContent"] = {"result": json.dumps(data)} if encoding == "wrapped" else data
        return httpx.Response(200, json={"result": result})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(gateway))) as client:
        response = client.post("/api/run", json={"tool": "get_latest_news", "arguments": {"limit": 10}})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["result"] == data


@pytest.mark.parametrize("value", ["", "{}", "null", "[]", '{"success":true,"data":{}}'])
def test_malformed_wrapped_trends_is_still_rejected(value):
    def gateway(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        return httpx.Response(200, json={"result": {"structuredContent": {"result": value}}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(gateway))) as client:
        response = client.post("/api/run", json={"tool": "get_latest_news"})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_response"


def test_wrapped_tool_failure_remains_a_failure():
    def gateway(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        data = {"success": False, "error": {"code": "no_content"}}
        return httpx.Response(200, json={"result": {"structuredContent": {"result": json.dumps(data)}}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(gateway))) as client:
        response = client.post("/api/run", json={"tool": "get_latest_news"})
        assert response.status_code == 422
        assert response.json()["ok"] is False
        assert response.json()["error"]["code"] == "no_content"


def test_wrapped_markdown_still_supports_document_tools():
    def gateway(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        return httpx.Response(200, json={"result": {"structuredContent": {"result": "# 文档标题\n\n正文"}}})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(gateway))) as client:
        response = client.post(
            "/api/run", json={"tool": "query-docs", "arguments": {"libraryId": "/org/library", "query": "用法"}}
        )
        assert response.status_code == 200
        assert "<h1>文档标题</h1>" in response.json()["rendered_html"]
