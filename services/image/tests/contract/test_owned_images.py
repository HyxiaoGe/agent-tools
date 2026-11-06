"""/v2/mcp 的 generate_image 和 /v1/images/{id}：图存在服务里、归产品，只能用本产品的密钥按 id 取、删。"""

from __future__ import annotations

import base64
import json
import struct
import uuid
import zlib

import httpx
import pytest
from fastmcp import Client

from src.config import ProductConfig, Settings
from src.mcp.owned import init_owned_mcp, owned_mcp
from src.models.request import GenerateRequest
from src.services.generator import _resolve_fallback, _supports_aspect_ratio, closest_aspect_ratio, tool_aspect_ratios
from tests.conftest import FAKE_PNG_B64, MockTransport

OTHER_KEY = "other-service-secret"
FILTERED = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}


def png(width: int, height: int) -> bytes:
    """宽高对的最小 PNG（服务只读头里的宽高）。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.fixture
def other_product(app, monkeypatch):
    app.state.settings.products["other"] = ProductConfig(
        service_key_env="TEST_OTHER_SERVICE_KEY", ark_key_env="TEST_ARK_API_KEY", ark_key_alias="other"
    )
    monkeypatch.setenv("TEST_OTHER_SERVICE_KEY", OTHER_KEY)
    return {"Authorization": "Bearer " + OTHER_KEY}


async def call(app, **args):
    async with Client(owned_mcp) as c:
        return await c.call_tool("generate_image", {"prompt": "a lighthouse", **args})


def text(result) -> str:
    return "\n".join(p.text for p in result.content if hasattr(p, "text"))


def sent_json(transport: MockTransport) -> dict:
    return json.loads(transport.last_request.content)


async def test_generated_image_belongs_to_the_caller_and_has_no_address(app, client, mock_transport, tmp_storage):
    result = await call(app)

    sc = result.structured_content
    assert sc["ok"] is True and result.is_error is False
    image = sc["image"]
    assert uuid.UUID(image["id"])
    assert (image["mime_type"], image["width"], image["height"]) == ("image/png", 1, 1)
    assert image["model"] == "gemini-3-pro-image-preview" and image["edited_from"] is None
    assert image["bytes"] == len(base64.b64decode(FAKE_PNG_B64))
    assert sc["cached"] is False
    # 给模型的文字：有 id，没有地址、没有模型名
    assert f"Image id: {image['id']}" in text(result)
    assert "http" not in text(result) and "gemini" not in text(result)
    # 不进公开的静态目录
    assert list(tmp_storage.iterdir()) == []

    resp = await client.get(f"/v1/images/{image['id']}")
    assert resp.status_code == 200
    assert resp.content == base64.b64decode(FAKE_PNG_B64)
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


async def test_grounding_is_always_on(app, mock_transport):
    await call(app)
    assert sent_json(mock_transport)["tools"] == [{"googleSearch": {}}]


async def test_other_products_cannot_fetch_or_delete(app, client, other_product):
    image_id = (await call(app)).structured_content["image"]["id"]

    assert (await client.get(f"/v1/images/{image_id}", headers=other_product)).status_code == 404
    assert (await client.delete(f"/v1/images/{image_id}", headers=other_product)).status_code == 204
    assert (await client.get(f"/v1/images/{image_id}")).status_code == 200  # 没被删


async def test_image_routes_need_a_product_key(app, client):
    image_id = (await call(app)).structured_content["image"]["id"]
    for method in ("GET", "DELETE"):
        resp = await client.request(method, f"/v1/images/{image_id}", headers={"Authorization": ""})
        assert resp.status_code == 401


async def test_delete_is_idempotent(app, client):
    image_id = (await call(app)).structured_content["image"]["id"]
    store = app.state.image_store
    path = store.path(await store.get("test", image_id))

    assert (await client.delete(f"/v1/images/{image_id}")).status_code == 204
    assert not path.exists()
    assert (await client.delete(f"/v1/images/{image_id}")).status_code == 204
    assert (await client.get(f"/v1/images/{image_id}")).status_code == 404
    assert (await client.get("/v1/images/not-an-id")).status_code == 404


async def test_cache_hit_is_a_copy_with_its_own_id(app, client, mock_transport):
    first = (await call(app)).structured_content
    second = (await call(app)).structured_content

    assert mock_transport.call_count == 1
    assert second["cached"] is True and second["image"]["id"] != first["image"]["id"]
    # 删一张不影响另一张（两段对话各自删各自的）
    assert (await client.delete(f"/v1/images/{first['image']['id']}")).status_code == 204
    assert (await client.get(f"/v1/images/{second['image']['id']}")).status_code == 200


async def test_cached_image_that_was_deleted_counts_as_a_miss(app, client, mock_transport):
    first = (await call(app)).structured_content
    await client.delete(f"/v1/images/{first['image']['id']}")

    again = (await call(app)).structured_content
    assert again["ok"] is True and again["cached"] is False
    assert mock_transport.call_count == 2


async def test_owned_cache_is_separate_from_the_public_one(app, client, mock_transport):
    legacy = await client.post("/v1/generate", json={"prompt": "a lighthouse", "grounding": True})
    assert legacy.status_code == 200

    result = (await call(app)).structured_content
    assert result["cached"] is False and mock_transport.call_count == 2


async def test_regenerate_skips_the_cache(app, mock_transport):
    await call(app)
    result = (await call(app, regenerate=True)).structured_content
    assert result["cached"] is False and mock_transport.call_count == 2


async def test_blank_prompt_never_reaches_upstream(app, client, mock_transport):
    result = await call(app, prompt="  \n\t ")
    assert result.structured_content["error"]["code"] == "invalid_params"
    assert "arguments were invalid" in text(result)

    resp = await client.post("/v1/generate", json={"prompt": "   "})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_params"
    assert mock_transport.call_count == 0


async def test_edit_reads_the_source_and_keeps_its_shape(app, mock_transport):
    data = png(1600, 900)
    source = await app.state.image_store.save(app_id="test", data=data, model="doubao-seedream-5-0-flash")

    result = await call(app, prompt="make the sky purple", source_image_id=source.id)

    sc = result.structured_content
    assert sc["ok"] is True and sc["image"]["edited_from"] == source.id
    sent = sent_json(mock_transport)
    assert sent["imageConfig"] == {"aspectRatio": "16:9"}
    content = sent["messages"][0]["content"]
    assert content[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(data).decode()
    assert "edited" in text(result) and "what was changed" in text(result)


async def test_edit_respects_an_explicit_ratio(app, mock_transport):
    source = await app.state.image_store.save(app_id="test", data=png(1600, 900), model="m")
    await call(app, source_image_id=source.id, aspect_ratio="1:1")
    assert sent_json(mock_transport)["imageConfig"] == {"aspectRatio": "1:1"}


@pytest.mark.parametrize("kind", ["unknown", "made_up", "other_product", "file_gone"])
async def test_unavailable_source_is_not_generated(app, mock_transport, kind):
    store = app.state.image_store
    if kind == "unknown":
        source_id = str(uuid.uuid4())
    elif kind == "made_up":
        source_id = "img_55d0d6a946b5e"  # Fusion 6b566d01：旧对话里模型自己编的编号
    elif kind == "other_product":
        source_id = (await store.save(app_id="other", data=png(10, 10), model="m")).id
    else:
        record = await store.save(app_id="test", data=png(10, 10), model="m")
        store.path(record).unlink()
        source_id = record.id

    result = await call(app, prompt="add a hat", source_image_id=source_id)

    err = result.structured_content["error"]
    assert (err["code"], err["retryable"]) == ("source_image_unavailable", False)
    assert "call this tool again without source_image_id" in text(result)
    assert mock_transport.call_count == 0


async def test_empty_source_id_means_a_new_image(app):
    result = (await call(app, source_image_id="  ")).structured_content
    assert result["ok"] is True and result["image"]["edited_from"] is None


async def test_failures_tell_the_model_what_to_do(app):
    init_owned_mcp(
        app.state.settings,
        app.state.cache,
        app.state.image_store,
        http_client=httpx.AsyncClient(transport=MockTransport(FILTERED)),
    )
    result = await call(app)

    assert result.structured_content == {
        "ok": False,
        "error": {
            "code": "content_filtered",
            "message": result.structured_content["error"]["message"],
            "retryable": False,
        },
    }
    assert "content safety filter" in text(result) and "do not retry" in text(result)
    assert "Error generating image" not in text(result)


async def test_tool_only_exposes_what_the_model_should_fill(app):
    async with Client(owned_mcp) as c:
        (tool,) = await c.list_tools()

    props = tool.input_schema["properties"]
    assert set(props) == {
        "prompt",
        "aspect_ratio",
        "style",
        "regenerate",
        "reference_subject",
        "quality",
        "source_image_id",
    }
    assert tool.input_schema["required"] == ["prompt"]
    # 出图要花钱、每次结果不同、调外部模型
    hints = tool.annotations
    assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint, hints.open_world_hint) == (
        False,
        False,
        False,
        True,
    )
    ratios = next(o["enum"] for o in props["aspect_ratio"]["anyOf"] if "enum" in o)
    assert tuple(ratios) == tool_aspect_ratios(app.state.settings)


def test_ratios_follow_the_fallback_chain():
    production = Settings(
        default_model="doubao-seedream-5-0-flash", default_fallback_model="gemini-nano-banana-2.1,qwen-image-3.0"
    )
    ratios = tool_aspect_ratios(production)
    assert len(ratios) == 14 and {"1:4", "4:1", "1:8", "8:1"} <= set(ratios)
    # 链上有只认 10 个比例的模型时只开这 10 个，主模型失败时一定换得了备用
    older = Settings(default_model="doubao-seedream-5-0-flash", default_fallback_model="gemini-3-pro-image-preview")
    assert len(tool_aspect_ratios(older)) == 10 and "8:1" not in tool_aspect_ratios(older)


def test_qwen_is_skipped_beyond_8_to_1():
    assert _supports_aspect_ratio("qwen-image-3.0", "8:1")
    assert not _supports_aspect_ratio("qwen-image-3.0", "16:1")
    settings = Settings(default_model="doubao-seedream-5-0-flash", default_fallback_model="qwen-image-3.0")
    chain, _ = _resolve_fallback(
        GenerateRequest(prompt="x", aspect_ratio="16:1"), "doubao-seedream-5-0-flash", settings
    )
    assert chain == []


@pytest.mark.parametrize(
    ("size", "expected"),
    [((1600, 900), "16:9"), ((900, 1600), "9:16"), ((1000, 1000), "1:1"), ((1200, 900), "4:3"), ((0, 10), "1:1")],
)
def test_closest_aspect_ratio(size, expected):
    ratios = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
    assert closest_aspect_ratio(*size, ratios) == expected


async def test_v2_over_http(app, client, fake_redis, monkeypatch):
    """经真的 HTTP 端点（挂在 /v2/mcp，旧的 /mcp 照旧）：要密钥，工具列表是新的那个。"""
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with app.router.lifespan_context(app):
        denied = await client.post("/v2/mcp", headers={**headers, "Authorization": ""}, json=body)
        assert denied.status_code == 401
        listed = await client.post("/v2/mcp", headers=headers, json=body)
        assert listed.status_code == 200, listed.text
        assert "source_image_id" in listed.text and "fallback_model" not in listed.text
        legacy = await client.post("/mcp", headers=headers, json=body)
        assert legacy.status_code == 200 and "fallback_model" in legacy.text
        called = await client.post(
            "/v2/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "generate_image", "arguments": {"prompt": "http cup"}},
            },
        )
        assert called.status_code == 200 and '"ok":true' in called.text.replace(" ", "")


@pytest.mark.parametrize("path", ["/mcp", "/v2/mcp"])
async def test_old_protocol_clients_still_handshake(app, client, fake_redis, monkeypatch, path):
    """服务升到新协议（2026-07-28）以后，按旧协议 initialize 的客户端（Fusion 自己写的、Claude Code）照样连得上。"""
    monkeypatch.setattr("src.main.aioredis.from_url", lambda *a, **kw: fake_redis)
    headers = {"Accept": "application/json, text/event-stream"}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}},
    }
    async with app.router.lifespan_context(app):
        response = await client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert '"protocolVersion":"2025-06-18"' in response.text.replace(" ", "")
    assert '"name":"image-service"' in response.text.replace(" ", "")


# ---- POST /v1/images：产品上传的图 ----


async def test_uploaded_image_belongs_to_the_caller(app, client, other_product, tmp_storage):
    data = png(1600, 900)

    resp = await client.post("/v1/images", content=data, headers={"Content-Type": "application/octet-stream"})

    assert resp.status_code == 201
    image = resp.json()
    assert uuid.UUID(image["id"])
    assert (image["mime_type"], image["width"], image["height"], image["bytes"]) == ("image/png", 1600, 900, len(data))
    assert (image["model"], image["edited_from"]) == ("upload", None)
    assert list(tmp_storage.iterdir()) == []  # 不进公开的静态目录
    fetched = await client.get(f"/v1/images/{image['id']}")
    assert (fetched.status_code, fetched.content) == (200, data)
    assert fetched.headers["content-type"] == "image/png"
    assert (await client.get(f"/v1/images/{image['id']}", headers=other_product)).status_code == 404
    assert (await client.delete(f"/v1/images/{image['id']}")).status_code == 204
    assert (await client.get(f"/v1/images/{image['id']}")).status_code == 404


async def test_uploaded_image_can_be_edited(app, client, mock_transport):
    data = png(900, 1600)
    image_id = (await client.post("/v1/images", content=data)).json()["id"]

    result = await call(app, prompt="add a red scarf", source_image_id=image_id)

    sc = result.structured_content
    assert sc["ok"] is True and sc["image"]["edited_from"] == image_id
    sent = sent_json(mock_transport)
    assert sent["imageConfig"] == {"aspectRatio": "9:16"}
    assert (
        sent["messages"][0]["content"][1]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(data).decode()
    )


async def test_upload_needs_a_product_key(client):
    resp = await client.post("/v1/images", content=png(10, 10), headers={"Authorization": ""})
    assert resp.status_code == 401


@pytest.mark.parametrize("data", [b"", b"GIF89a" + b"\x00" * 20, b"\x89PNG\r\n\x1a\n" + b"\x00" * 4])
async def test_upload_rejects_what_is_not_a_readable_image(app, client, data):
    resp = await client.post("/v1/images", content=data)

    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_image"
    assert list(app.state.image_store.directory.glob("*")) == []


async def test_upload_rejects_images_over_4_mb(app, client):
    from src.api.images import MAX_UPLOAD_BYTES

    big = png(4000, 3000) + b"\x00" * MAX_UPLOAD_BYTES

    declared = await client.post("/v1/images", content=big)

    async def chunks():  # 不带 Content-Length（分块传）也要卡住
        for i in range(0, len(big), 1024 * 1024):
            yield big[i : i + 1024 * 1024]

    streamed = await client.post("/v1/images", content=chunks())

    for resp in (declared, streamed):
        assert resp.status_code == 413
        assert resp.json()["error"] == "image_too_large"
    assert list(app.state.image_store.directory.glob("*")) == []
    exactly = png(10, 10)
    assert (
        await client.post("/v1/images", content=exactly + b"\x00" * (MAX_UPLOAD_BYTES - len(exactly)))
    ).status_code == 201
