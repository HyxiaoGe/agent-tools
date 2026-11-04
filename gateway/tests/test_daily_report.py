from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, date, datetime

import httpx
import pytest

from app import daily_report, feishu
from app.config import Settings
from app.usage import UsageRecord, UsageStore

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 31)
SEARCH_SUMMARY = {
    "day": "2026-10-31",
    "available": True,
    "providers": [
        {"provider": "deepseek", "role": "primary", "registered": True, "limit": 1000, "used": 312, "rejected": 0},
        {"provider": "firecrawl", "role": "fallback", "registered": True, "limit": 500, "used": 12, "rejected": 0},
        {"provider": "tavily", "role": "fallback", "registered": True, "limit": 100, "used": 0, "rejected": 2},
        {"provider": "brave", "role": "other", "registered": False, "limit": 100, "used": 0, "rejected": 0},
    ],
    "fallback": {"error": 2, "insufficient": 8},
    "firecrawl_remaining_credits": 69797,
    "firecrawl_billing_period_end": "2026-11-22T21:35:09.173Z",
}
IMAGE_SUMMARY = {
    "day": "2026-10-31",
    "available": True,
    "providers": [
        {"provider": "volcano", "limit": 100, "used": 2, "rejected": 0},
        {"provider": "google", "limit": 50, "used": 0, "rejected": 0},
        {"provider": "alibaba", "limit": None, "used": 1, "rejected": 0},
        {"provider": "tavily", "limit": 100, "used": 0, "rejected": 0},
    ],
}
READER_SUMMARY = {
    "day": "2026-10-31",
    "available": True,
    "providers": [
        {"provider": "firecrawl", "limit": 300, "used": 1, "rejected": 0},
        {"provider": "tavily", "limit": 300, "used": 0, "rejected": 0},
        {"provider": "jina", "limit": 300, "used": 0, "rejected": 1},
    ],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        usage_db_path=str(tmp_path / "usage.sqlite3"),
        daily_report_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
        daily_report_sign_secret="sign-secret",
        daily_report_state_path=str(tmp_path / "daily_report_state.json"),
    )


def _record(
    store: UsageStore, ts: str, service: str, path: str, *, tool=None, app_id="fusion", status=200, outcome="ok"
):
    store.record_sync(
        UsageRecord(
            ts=ts,
            app_id=app_id,
            user_id=None,
            scene=None,
            request_id=None,
            service=service,
            method="POST",
            path=path,
            tool=tool,
            status=status,
            outcome=outcome,
            duration_ms=10,
        )
    )


def _seed(settings: Settings) -> None:
    store = UsageStore(settings.usage_db_path, retention_days=3650)
    # 北京时间 10-31 = UTC 10-30 16:00 ~ 10-31 16:00
    _record(store, "2026-10-30T15:59:59.999999+00:00", "search", "/search")  # 北京 10-30 23:59，不算
    _record(store, "2026-10-30T16:00:00+00:00", "search", "/search")  # 北京 10-31 00:00，算
    _record(store, "2026-10-31T03:00:00+00:00", "search", "/search")
    _record(store, "2026-10-31T03:00:01+00:00", "search", "/search", status=502)
    _record(store, "2026-10-31T04:00:00+00:00", "reader", "/read", app_id="validation", outcome="timeout", status=504)
    # MCP 握手（initialize / tools/list / SSE）不算调用
    for _ in range(5):
        _record(store, "2026-10-31T05:00:00+00:00", "image", "/mcp/", app_id="validation")
    _record(store, "2026-10-31T05:00:00+00:00", "image", "/mcp/", tool="generate_image", app_id="lingdraft")
    _record(store, "2026-10-31T05:00:00+00:00", "image", "/v1/generate")
    # 按 id 取、删存着的图不算调用
    _record(store, "2026-10-31T05:00:00+00:00", "image", "/v1/images/3f0e8a4c-0000-4000-8000-000000000000")
    _record(store, "2026-10-31T06:00:00+00:00", "tmap", "/mcp", tool="placeSuggestion")
    _record(store, "2026-10-31T06:00:00+00:00", "tmap", "/mcp", tool="placeSuggestion")
    _record(store, "2026-10-31T06:00:00+00:00", "tmap", "/mcp", tool="placeSearchNearby")
    _record(store, "2026-10-31T15:59:59+00:00", "weather", "/v1/forecast/daily")  # 北京 10-31 23:59，算
    _record(store, "2026-10-31T16:00:00+00:00", "weather", "/v1/forecast/daily")  # 北京 11-01 00:00，不算
    store.close()


def _search_client(handler=None) -> httpx.AsyncClient:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json=SEARCH_SUMMARY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://search-service:8080")
    client.requests = requests  # type: ignore[attr-defined]
    return client


def _service_client(name: str, summary: dict, handler=None) -> httpx.AsyncClient:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request) if handler is not None else httpx.Response(200, json=summary)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=f"http://{name}")
    client.requests = requests  # type: ignore[attr-defined]
    return client


def _clients(search: httpx.AsyncClient | None = None, *, image=None, reader=None) -> dict[str, httpx.AsyncClient]:
    """日报用的各服务 client；image / reader 传 handler 可改返回，默认返回上面的计数。"""
    return {
        "search": search or _search_client(),
        "image": _service_client("image", IMAGE_SUMMARY, image),
        "reader": _service_client("reader", READER_SUMMARY, reader),
    }


class FakeFeishu:
    """替换 httpx.AsyncClient，只拦截发往飞书的请求。"""

    def __init__(self, monkeypatch, *, status: int = 200, code: int = 0, error: Exception | None = None):
        self.posted: list[dict] = []
        fake = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url: str, json: dict):
                if error is not None:
                    raise error
                fake.posted.append({"url": url, "json": json})
                return httpx.Response(status, json={"code": code, "msg": "x"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(feishu, "_feishu_client", _Client)


def test_day_bounds_follow_beijing_midnight():
    assert daily_report.day_bounds_utc(DAY) == ("2026-10-30T16:00:00+00:00", "2026-10-31T16:00:00+00:00")


def test_collect_day_stats_skips_mcp_handshakes_and_respects_day_bounds(settings):
    _seed(settings)

    stats = daily_report.collect_day_stats(settings.usage_db_path, DAY)

    assert stats.calls == 10
    assert stats.failed == 2
    assert {name: (s.calls, s.failed) for name, s in stats.services.items()} == {
        "search": (3, 1),
        "reader": (1, 1),
        "image": (2, 0),
        "tmap": (3, 0),
        "weather": (1, 0),
    }
    assert stats.services["image"].tools == {"generate_image": 1}
    assert dict(stats.products) == {"fusion": 8, "validation": 1, "lingdraft": 1}


def test_collect_day_stats_handles_missing_database(tmp_path):
    assert daily_report.collect_day_stats(str(tmp_path / "nope.sqlite3"), DAY).calls == 0


async def test_build_report_combines_gateway_counts_with_search_upstreams(settings):
    _seed(settings)
    clients = _clients()
    client = clients["search"]

    text = await daily_report.build_report(settings, clients, DAY)

    assert client.requests[0].url.path == "/usage/daily"
    assert client.requests[0].url.params["day"] == "2026-10-31"
    [image_request] = clients["image"].requests
    assert (image_request.url.path, image_request.url.params["day"]) == ("/v1/quota", "2026-10-31")
    [reader_request] = clients["reader"].requests
    assert (reader_request.url.path, reader_request.url.params["day"]) == ("/usage/daily", "2026-10-31")
    assert text == "\n".join(
        [
            "【agent-tools 日报】10-31（北京时间）",
            "调用 10 次，失败 2 次（前一天 1 次）",
            "按服务（调用 / 失败）：",
            "· search 3 / 1",
            "· tmap 3 / 0（placeSuggestion 2、placeSearchNearby 1）",
            "· image 2 / 0（generate_image 1）",
            "· reader 1 / 1",
            "· weather 1 / 0",
            "按产品：fusion 8 · lingdraft 1 · validation 1",
            "search 上游（调用 / 每日上限）：",
            "· deepseek（主） 312 / 1000",
            "· firecrawl（备用） 12 / 500",
            "· tavily（备用） 0 / 100，被额度拦下 2 次",
            "· 转备用 10 次（报错 2，结果不足 8）",
            # 计费周期结束 11-22 21:35 UTC = 北京 11-23
            "Firecrawl 剩余 69,797 credits（计费周期至 11-23）",
            "image 上游（调用 / 每日上限）：volcano 2 / 100 · alibaba 1（不限额）",
            "reader 上游（调用 / 每日上限）：firecrawl 1 / 300 · jina 0 / 300（拦下 1 次）",
        ]
    )
    for c in clients.values():
        await c.aclose()


async def test_build_report_degrades_when_search_summary_unavailable(settings):
    _seed(settings)
    client = _search_client(lambda _r: httpx.Response(503, text="down"))

    text = await daily_report.build_report(settings, _clients(client), DAY)

    assert "\nsearch 上游：获取失败\n" in text
    await client.aclose()


async def test_build_report_explains_missing_search_counts(settings):
    _seed(settings)
    empty = {**SEARCH_SUMMARY, "providers": [{**p, "used": 0, "rejected": 0} for p in SEARCH_SUMMARY["providers"]]}
    empty["fallback"] = {"error": 0, "insufficient": 0}
    empty["firecrawl_remaining_credits"] = None
    client = _search_client(lambda _r: httpx.Response(200, json=empty))

    text = await daily_report.build_report(settings, _clients(client), DAY)

    assert "\nsearch 上游：没有调用上游（全部命中缓存，或计数已过 3 天保留期）\n" in text
    await client.aclose()


async def test_build_report_degrades_per_service_when_quota_summaries_fail(settings, caplog):
    caplog.set_level(logging.WARNING, logger="gateway")
    _seed(settings)

    def boom(_request):
        raise httpx.ConnectError("image down")

    clients = _clients(image=boom, reader=lambda _r: httpx.Response(200, json={**READER_SUMMARY, "available": False}))
    text = await daily_report.build_report(settings, clients, DAY)

    assert text.splitlines()[-2:] == ["image 上游：获取失败", "reader 上游：计数不可用"]
    assert "daily_report_upstream_summary_failed: image ConnectError" in caplog.text
    # search 照常
    assert "· deepseek（主） 312 / 1000" in text

    missing = {"search": _search_client()}
    assert (await daily_report.build_report(settings, missing, DAY)).splitlines()[-2:] == [
        "image 上游：获取失败",
        "reader 上游：获取失败",
    ]


async def test_quota_lines_explain_missing_counts_and_skip_idle_services(settings):
    _seed(settings)
    idle_image = {**IMAGE_SUMMARY, "providers": [{**p, "used": 0} for p in IMAGE_SUMMARY["providers"]]}
    idle_reader = {
        **READER_SUMMARY,
        "providers": [{**p, "used": 0, "rejected": 0} for p in READER_SUMMARY["providers"]],
    }
    clients = _clients(
        image=lambda _r: httpx.Response(200, json=idle_image),
        reader=lambda _r: httpx.Response(200, json=idle_reader),
    )

    text = await daily_report.build_report(settings, clients, DAY)
    assert text.splitlines()[-2:] == [
        "image 上游：没有调用上游（全部命中缓存，或计数已过 3 天保留期）",
        "reader 上游：没有调用上游（全部直接抓取，或计数已过 3 天保留期）",
    ]

    # 前一天 image / reader 都没被调用：没有上游计数就不出这两行
    quiet = await daily_report.build_report(settings, clients, date(2026, 10, 30))
    assert "image 上游" not in quiet and "reader 上游" not in quiet


def test_due_report_day_waits_until_configured_beijing_hour():
    assert daily_report.due_report_day(datetime(2026, 11, 1, 0, 59, tzinfo=UTC), 9) is None  # 北京 08:59
    assert daily_report.due_report_day(datetime(2026, 11, 1, 1, 0, tzinfo=UTC), 9) == DAY  # 北京 09:00
    assert daily_report.due_report_day(datetime(2026, 11, 1, 15, 59, tzinfo=UTC), 9) == DAY  # 北京 23:59


async def test_run_due_report_sends_signed_message_once_per_day(settings, monkeypatch):
    _seed(settings)
    feishu = FakeFeishu(monkeypatch)
    clients = _clients()

    assert await daily_report.run_due_report(settings, clients, datetime(2026, 11, 1, 0, 30, tzinfo=UTC)) is None
    assert await daily_report.run_due_report(settings, clients, datetime(2026, 11, 1, 1, 5, tzinfo=UTC)) == "sent"
    assert await daily_report.run_due_report(settings, clients, datetime(2026, 11, 1, 1, 10, tzinfo=UTC)) is None

    assert len(feishu.posted) == 1
    sent = feishu.posted[0]
    assert sent["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
    assert sent["json"]["content"]["text"].startswith("【agent-tools 日报】10-31")
    expected = base64.b64encode(
        hmac.new(f"{sent['json']['timestamp']}\nsign-secret".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert sent["json"]["sign"] == expected
    state = json.loads(open(settings.daily_report_state_path).read())
    assert state["2026-10-31"] == {"attempts": 1, "status": "sent"}
    for client in clients.values():
        await client.aclose()


async def test_run_due_report_retries_failed_send_at_most_three_times(settings, monkeypatch):
    _seed(settings)
    feishu = FakeFeishu(monkeypatch, code=19021)
    client = _search_client()
    now = datetime(2026, 11, 1, 1, 5, tzinfo=UTC)

    results = [await daily_report.run_due_report(settings, _clients(client), now) for _ in range(5)]

    assert results == ["failed", "failed", "failed", None, None]
    assert len(feishu.posted) == 3
    await client.aclose()


async def test_send_failure_logs_never_contain_the_webhook(settings, monkeypatch, caplog):
    error = httpx.ConnectError(f"connect failed for {settings.daily_report_webhook.get_secret_value()}")
    FakeFeishu(monkeypatch, error=error)
    caplog.set_level(logging.INFO, logger="gateway")

    ok = await feishu.send_feishu_text(
        settings.daily_report_webhook.get_secret_value(), "", "hi", purpose="daily_report"
    )

    assert ok is False
    assert "secret-token" not in caplog.text
    assert "ConnectError" in caplog.text


async def test_manual_test_send_prefixes_title_and_does_not_touch_schedule_state(settings, monkeypatch):
    _seed(settings)
    feishu = FakeFeishu(monkeypatch)
    client = _search_client()

    assert await daily_report.send_daily_report(settings, _clients(client), DAY, test=True) is True

    assert feishu.posted[0]["json"]["content"]["text"].startswith("【测试】【agent-tools 日报】10-31")
    assert not os.path.exists(settings.daily_report_state_path)
    await client.aclose()


def test_webhook_is_hidden_in_settings_repr(settings):
    assert "secret-token" not in repr(settings)
    assert "sign-secret" not in repr(settings)
