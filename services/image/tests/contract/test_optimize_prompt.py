"""验证提示词优化的接口契约、模型兼容、缓存及备用请求体。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from src.mcp.server import init_mcp
from tests.conftest import MockTransport, make_gemini_response, make_seedream_response

PRO = "doubao-seedream-5-0-pro"
FLASH = "doubao-seedream-5-0-flash"
LITE = "doubao-seedream-5-0-lite"
GEMINI = "gemini-3-pro-image-preview"
QWEN = "qwen-image-3.0"
ALIASES = ["doubao-seedream-4-0", "doubao-seedream-4-5"]


async def _request(client, app, entry, body):
    if entry == "rest":
        response = await client.post("/v1/generate", json=body)
        return response.status_code, response.json()
    async with Client(app.state.mcp) as mcp_client:
        result = await mcp_client.call_tool("generate_image", body)
    data = result.structured_content
    return (200 if data["ok"] else 422), data if data["ok"] else data["error"]


@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize(
    "model,options,expected",
    [(PRO, {"mode": "fast"}, {"mode": "fast"})]
    + [(m, {"mode": "standard"}, {"mode": "standard"}) for m in [PRO, FLASH, LITE, *ALIASES]]
    + [(PRO, {}, {"mode": "standard"}), (PRO, None, None)],
)
async def test_prompt_options_upstream_body(client, app, entry, model, options, expected):
    transport = MockTransport(make_seedream_response())
    async with httpx.AsyncClient(transport=transport) as upstream:
        app.state.http_client = upstream
        init_mcp(app.state.settings, app.state.cache, http_client=upstream)
        status, data = await _request(
            client, app, entry, {"prompt": "咖啡海报", "model": model, "optimize_prompt_options": options}
        )
    assert status == 200
    assert data["model"] == model
    wire = json.loads(transport.last_request.content)
    if expected is None:
        assert "optimize_prompt_options" not in wire
    else:
        assert wire["optimize_prompt_options"] == expected


@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize(
    "model,mode",
    [(m, "fast") for m in [FLASH, LITE, *ALIASES, GEMINI, QWEN]] + [(GEMINI, "standard"), (QWEN, "standard")],
)
async def test_incompatible_options_rejected_before_upstream(client, app, routing, entry, model, mode):
    status, data = await _request(
        client, app, entry, {"prompt": "咖啡海报", "model": model, "optimize_prompt_options": {"mode": mode}}
    )
    assert status == 422
    assert data.get("error", data.get("code")) == "invalid_params"
    assert data["fields"] == ["optimize_prompt_options"]
    assert not any(routing.calls.values())


@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize("options", [{"mode": "off"}, {"mode": None}, {"enabled": True}, "fast", []])
async def test_invalid_options_schema(client, app, routing, entry, options):
    body = {"prompt": "海报", "model": PRO, "optimize_prompt_options": options}
    if entry == "rest":
        response = await client.post("/v1/generate", json=body)
        assert response.status_code == 422
        assert response.json()["fields"] == ["optimize_prompt_options"]
    else:
        async with Client(app.state.mcp) as mcp_client:
            result = await mcp_client.call_tool("generate_image", body, raise_on_error=False)
        assert result.is_error
    assert not any(routing.calls.values())


@pytest.mark.parametrize("alias", ALIASES)
async def test_fast_checks_actual_legacy_wire_id(client, app, alias):
    app.state.settings.ark_model_id = "doubao-seedream-4-0-250828"
    transport = MockTransport(make_seedream_response())
    async with httpx.AsyncClient(transport=transport) as upstream:
        app.state.http_client = upstream
        response = await client.post(
            "/v1/generate", json={"prompt": "海报", "model": alias, "optimize_prompt_options": {"mode": "fast"}}
        )
    assert response.status_code == 200
    wire = json.loads(transport.last_request.content)
    assert wire["model"] == "doubao-seedream-4-0-250828"
    assert wire["optimize_prompt_options"] == {"mode": "fast"}


async def test_fast_cache_is_separate_and_standard_shares_default(client, app, routing):
    body = {"prompt": "咖啡海报", "quality": "high"}
    responses = []
    for options in [None, {"mode": "standard"}, {}, {"mode": "fast"}, {"mode": "fast"}]:
        response = await client.post("/v1/generate", json={**body, "optimize_prompt_options": options})
        assert response.status_code == 200
        responses.append(response.json())
    default, standard, empty, fast, again = responses
    assert default["id"] == standard["id"] == empty["id"]
    assert fast["id"] == again["id"] != default["id"]
    assert [r["cached"] for r in responses] == [False, True, True, False, True]
    assert routing.calls == {"gemini": 0, "ark": 2}


@pytest.mark.parametrize("fallback", [FLASH, LITE, GEMINI, QWEN, *ALIASES])
async def test_fast_does_not_leak_to_incompatible_fallback(client, app, fallback):
    wires = []

    async def respond(request):
        wire = json.loads(request.content)
        wires.append(wire)
        if len(wires) == 1:
            return httpx.Response(403, json={"error": {"code": "AccountOverdueError", "message": "overdue"}})
        if fallback == QWEN:
            # 请求体已捕获，模拟失败以避免触发后续图片下载。
            return httpx.Response(400, json={"code": "InvalidParameter", "message": "test"})
        return httpx.Response(200, json=make_gemini_response() if fallback == GEMINI else make_seedream_response())

    app.state.settings.dashscope_api_key = "test-key"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
        app.state.http_client = upstream
        response = await client.post(
            "/v1/generate",
            json={
                "prompt": "海报",
                "model": PRO,
                "fallback_model": fallback,
                "optimize_prompt_options": {"mode": "fast"},
            },
        )
    assert response.status_code == (502 if fallback == QWEN else 200)
    assert len(wires) == 2
    assert wires[0]["optimize_prompt_options"] == {"mode": "fast"}
    assert "optimize_prompt_options" not in wires[1]
    if fallback != QWEN:
        assert response.json()["model"] == fallback
        assert response.json()["fallback_used"] is True


async def test_rest_and_mcp_expose_optional_options_schema(client, app):
    response = await client.get("/openapi.json")
    request_schema = response.json()["components"]["schemas"]["GenerateRequest"]
    assert "optimize_prompt_options" in request_schema["properties"]
    assert "optimize_prompt_options" not in request_schema["required"]
    async with Client(app.state.mcp) as mcp_client:
        tools = await mcp_client.list_tools()
    schema = next(t for t in tools if t.name == "generate_image").inputSchema
    assert "optimize_prompt_options" not in schema["required"]
    options = schema["properties"]["optimize_prompt_options"]
    assert any(s.get("type") == "null" for s in options["anyOf"])
    object_schema = next(s for s in options["anyOf"] if s.get("type") == "object")
    assert object_schema["properties"]["mode"]["enum"] == ["standard", "fast"]
