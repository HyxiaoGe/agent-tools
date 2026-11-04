"""服务入口和 MCP：经 HTTP 调工具（和网关转来的一样带 x-caller-app、_meta 里的设备位置），
给模型的文字、健康检查、用量、日志。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastmcp import Client

from app.main import HTTP_CLIENT_LOGGERS, build_service, configure_logging, create_app
from app.mcp_server import NO_RESULT, PLACE_DESCRIPTION, ROUTE_DESCRIPTION, build_mcp
from app.tools import GEOLOCATION_META_KEY
from app.upstream import QuotaMemo, Usage
from tests.fake import AMAP_QUOTA, KEYS, TMAP_QUOTA, FakeUpstream, settings

pytestmark = pytest.mark.anyio
HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def rpc(response: httpx.Response) -> dict[str, Any]:
    text = response.text
    return json.loads(text.split("data: ", 1)[1]) if text.startswith("event") else response.json()


async def call(fake: FakeUpstream, name: str, arguments: dict[str, Any], meta: dict | None = None):
    mcp = build_mcp(build_service(settings(), fake.client(), Usage(), QuotaMemo()))
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments, raise_on_error=False, meta=meta)


class TestHttp:
    async def test_tools_over_http_with_caller_and_device(self, tmp_path):
        fake = FakeUpstream().sample("amap", "convert").sample("amap", "place_around")
        app = create_app(settings(usage_state_path=str(tmp_path / "usage.json")), fake.transport())
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "place_search",
                "arguments": {"query": "咖啡", "anchor_source": "current_location", "limit": 2},
                "_meta": {GEOLOCATION_META_KEY: {"latitude": 31.242, "longitude": 121.4855, "accuracy_m": 30}},
            },
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://maps") as http:
                listed = rpc(
                    await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=HEADERS)
                )
                tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
                assert set(tools) == {"place_search", "route_compare"}
                assert tools["place_search"]["inputSchema"]["required"] == ["query", "anchor_source"]
                assert tools["route_compare"]["inputSchema"]["required"] == [
                    "origin",
                    "destination",
                    "origin_source",
                    "destination_source",
                    "modes",
                ]
                assert all(tool["annotations"]["readOnlyHint"] for tool in tools.values())

                called = rpc(await http.post("/mcp", json=body, headers={**HEADERS, "x-caller-app": "lingdraft"}))
                result = called["result"]
                assert result["isError"] is False
                places = result["structuredContent"]["places"]
                assert len(places) == 2 and "src=lingdraft" in places[0]["map_url"]
                # 当前位置先换成 GCJ-02 再搜周边
                assert fake.params("amap", "/v3/assistant/coordinate/convert") == [
                    {"locations": "121.485500,31.242000", "coordsys": "gps"}
                ]
                assert fake.params("amap", "/v5/place/around")[0]["location"] == "121.489984,31.240030"

                assert (await http.get("/health")).json() == {"status": "ok", "providers": ["amap", "tmap"]}
                usage = (await http.get("/usage/daily")).json()
                assert usage["available"] is True and usage["fallbacks"] == 0
                assert usage["providers"] == [
                    {"provider": "amap", "requests": 2, "failed": 0, "quota": 0, "skipped": 0}
                ]
                old = (await http.get("/usage/daily", params={"day": "2026-01-01"})).json()
                assert old == {"day": "2026-01-01", "available": True, "providers": [], "fallbacks": 0}
        saved = json.loads((tmp_path / "usage.json").read_text())
        assert list(saved.values())[0]["amap"]["requests"] == 2

    async def test_without_keys_starts_and_answers_no_result(self):
        app = create_app(settings(amap_api_key="", tmap_api_key=""), FakeUpstream().transport())
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "place_search", "arguments": {"query": "咖啡", "anchor_source": "none"}},
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://maps") as http:
                assert (await http.get("/health")).json()["providers"] == []
                result = rpc(await http.post("/mcp", json=body, headers=HEADERS))["result"]
                assert result["structuredContent"]["error"]["code"] == "unavailable"
                assert result["content"][0]["text"] == NO_RESULT


class TestModelText:
    async def test_success_text_hides_links_photos_and_providers(self):
        fake = FakeUpstream().sample("amap", "place_text")
        result = await call(fake, "place_search", {"query": "火锅", "anchor_source": "none", "city": "上海"})
        text = result.content[0].text
        data = json.loads(text)
        assert "ok" not in data and "providers" not in data
        assert all(key not in text for key in ("photo_url", "map_url", "provider", "autonavi"))
        assert data["places"][0]["place_id"] == "amap_B0FFKKRXAZ"
        assert result.structured_content["places"][0]["photo_url"].startswith("https://")

    async def test_ambiguous_text_lists_numbered_candidates(self):
        fake = FakeUpstream().sample("amap", "geo_gulou")
        result = await call(
            fake,
            "route_compare",
            {
                "origin": "鼓楼",
                "destination": "鼓楼",
                "origin_source": "named",
                "destination_source": "named",
                "modes": ["walking"],
            },
        )
        error = result.structured_content["error"]
        assert error["code"] == "ambiguous_location" and error["retryable"] is True
        assert error["candidate_argument"] == "origin_candidate"
        assert [c["candidate"] for c in error["candidates"]] == [1, 2, 3, 4]
        text = result.content[0].text
        assert '"candidate_argument": "origin_candidate"' in text and "南京市" in text
        assert "place_id" in text  # 路线工具多一种情况：用搜索结果里的编号
        assert result.is_error is False

    async def test_quota_text_does_not_talk_about_the_tool(self):
        fake = FakeUpstream().on("amap", "/v5/place/text", AMAP_QUOTA).on("tmap", "/ws/place/v1/search", TMAP_QUOTA)
        result = await call(fake, "place_search", {"query": "火锅", "anchor_source": "none", "city": "上海"})
        assert result.content[0].text == NO_RESULT
        error = result.structured_content["error"]
        assert error["code"] == "quota_exhausted" and error["retryable"] is False and "reset_at" in error

    async def test_bugs_become_no_result_with_a_stack_in_the_log(self, caplog):
        def broken(request):
            raise RuntimeError("bug")

        fake = FakeUpstream().on("amap", "/v5/place/text", broken)
        result = await call(fake, "place_search", {"query": "火锅", "anchor_source": "none"})
        assert result.content[0].text == NO_RESULT
        assert any(r.getMessage() == "place_search_crashed" and r.exc_info for r in caplog.records)

    async def test_bad_device_meta_is_ignored(self):
        meta = {GEOLOCATION_META_KEY: {"latitude": 31.2, "longitude": 121.4, "accuracy_m": 900_000}}
        result = await call(
            FakeUpstream(), "place_search", {"query": "咖啡", "anchor_source": "current_location"}, meta
        )
        assert result.structured_content["error"]["code"] == "location_unavailable"

    def test_descriptions_are_product_neutral(self):
        for description in (PLACE_DESCRIPTION, ROUTE_DESCRIPTION):
            assert description.isascii()
            for word in ("Amap", "Tencent", "高德", "腾讯", "card", "place_search", "route_compare", "web_search"):
                assert word not in description


class TestState:
    def test_usage_survives_restart_and_keeps_three_days(self, tmp_path):
        path = str(tmp_path / "usage.json")
        now = {"value": datetime(2026, 11, 1, 4, tzinfo=UTC)}
        usage = Usage(path, clock=lambda: now["value"])
        usage.count("amap", "requests")
        usage.fallback()
        now["value"] += timedelta(days=4)
        usage.count("tmap", "requests")
        reloaded = Usage(path, clock=lambda: now["value"])
        assert reloaded.summary(date(2026, 11, 1))["providers"] == []  # 超过 3 天的删了
        assert reloaded.summary(date(2026, 11, 5))["providers"][0]["provider"] == "tmap"

    def test_unreadable_state_starts_from_zero(self, tmp_path, caplog):
        path = tmp_path / "usage.json"
        path.write_text("{broken")
        assert Usage(str(path)).summary(date(2026, 11, 4))["providers"] == []
        assert "maps_usage_state_unreadable" in caplog.text

    def test_quota_memo_rechecks_after_an_hour(self):
        now = {"value": datetime(2026, 11, 4, 2, tzinfo=UTC)}
        memo = QuotaMemo(clock=lambda: now["value"])
        reset = datetime(2026, 12, 1, tzinfo=UTC)
        memo.mark("amap", "search", reset)
        assert memo.blocked("amap", "search") == reset and memo.blocked("amap", "geocode") is None
        now["value"] += timedelta(minutes=61)
        assert memo.blocked("amap", "search") is None

    def test_http_client_logs_are_silenced(self):
        configure_logging()
        assert all(logging.getLogger(name).level == logging.WARNING for name in HTTP_CLIENT_LOGGERS)
        assert KEYS["amap"] not in repr(settings())
