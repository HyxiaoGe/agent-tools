from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, date, datetime

import httpx
import pytest

from app import daily_report, feishu
from app.config import ProductConfig, Settings, parse_daily_limits
from app.limits import DailyLimiter, _seconds_until_reset
from app.main import create_app
from app.usage import UsageRecord, UsageStore

pytestmark = pytest.mark.anyio

FUSION_KEY = "fusion-test-key"
SERVICES = ("image", "search", "reader", "trends", "amap", "context7", "weather", "flyai", "tmap", "maps")
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
TOOL_CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_latest_news"}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def product_keys(monkeypatch):
    monkeypatch.setenv("FUSION_SERVICE_KEY", FUSION_KEY)


def make_settings(tmp_path, limits: str, **extra) -> Settings:
    return Settings(
        _env_file=None,
        products={"fusion": ProductConfig(service_key_env="FUSION_SERVICE_KEY", services=list(SERVICES))},
        usage_db_path=str(tmp_path / "usage.sqlite3"),
        service_daily_limits=limits,
        **extra,
    )


class App:
    """起一个带 mock 上游的网关，记录每个上游实际收到的请求数。"""

    def __init__(self, settings: Settings):
        self.received: dict[str, int] = {name: 0 for name in SERVICES}

        def transport(service: str) -> httpx.MockTransport:
            def handle(_request: httpx.Request) -> httpx.Response:
                self.received[service] += 1
                return httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json"})

            return httpx.MockTransport(handle)

        self.app = create_app(settings, {name: transport(name) for name in SERVICES})

    async def __aenter__(self) -> httpx.AsyncClient:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gateway")
        return self._http

    async def __aexit__(self, *exc):
        await self._http.aclose()
        await self._lifespan.__aexit__(*exc)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {FUSION_KEY}"}


def usage_rows(settings: Settings) -> list[dict]:
    conn = sqlite3.connect(settings.usage_db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("SELECT * FROM requests ORDER BY id")]
    conn.close()
    return rows


def _record(store: UsageStore, ts: str, service: str, *, outcome: str = "ok", status: int = 200) -> None:
    store.record_sync(
        UsageRecord(
            ts=ts,
            app_id="fusion",
            user_id=None,
            scene=None,
            request_id=None,
            service=service,
            method="POST",
            path="/search",
            tool=None,
            status=status,
            outcome=outcome,
            duration_ms=1,
        )
    )


def test_parse_daily_limits():
    assert parse_daily_limits(" Search=2000, image=0 ,") == {"search": 2000, "image": 0}
    for raw in ("search", "search=-1", "search=many", "=3"):
        with pytest.raises(ValueError):
            parse_daily_limits(raw)


def test_unknown_service_in_limits_fails_startup(tmp_path):
    with pytest.raises(ValueError, match="unknown services"):
        make_settings(tmp_path, "search=10,serach=5")


async def test_service_over_daily_limit_gets_429_and_is_recorded(tmp_path):
    settings = make_settings(tmp_path, "search=2")
    gateway = App(settings)
    async with gateway as http:
        statuses = [(await http.post("/search/search", headers=auth(), json={"query": "q"})) for _ in range(3)]

    assert [r.status_code for r in statuses] == [200, 200, 429]
    rejected = statuses[2]
    assert rejected.json()["error"] == "daily_limit_exceeded"
    assert 1 <= int(rejected.headers["Retry-After"]) <= 86400
    assert gateway.received["search"] == 2
    rows = usage_rows(settings)
    assert [(r["status"], r["outcome"]) for r in rows] == [(200, "ok"), (200, "ok"), (429, "limited")]


async def test_mcp_handshakes_are_neither_counted_nor_blocked(tmp_path):
    gateway = App(make_settings(tmp_path, "trends=1"))
    async with gateway as http:
        handshakes = [await http.post("/trends/mcp", headers=auth(), json=INITIALIZE) for _ in range(3)]
        first_call = await http.post("/trends/mcp", headers=auth(), json=TOOL_CALL)
        second_call = await http.post("/trends/mcp", headers=auth(), json=TOOL_CALL)
        handshake_after_limit = await http.post("/trends/mcp", headers=auth(), json=INITIALIZE)

    assert [r.status_code for r in handshakes] == [200, 200, 200]
    assert first_call.status_code == 200
    assert second_call.status_code == 429
    assert handshake_after_limit.status_code == 200


async def test_fetching_and_deleting_stored_images_is_neither_counted_nor_blocked(tmp_path):
    gateway = App(make_settings(tmp_path, "image=1"))
    image = "/image/v1/images/3f0e8a4c-0000-4000-8000-000000000000"
    generate_call = {**TOOL_CALL, "params": {"name": "generate_image"}}
    async with gateway as http:
        fetches = [(await http.get(image, headers=auth())).status_code for _ in range(3)]
        generate = await http.post("/image/v2/mcp", headers=auth(), json=generate_call)
        over = await http.post("/image/v2/mcp", headers=auth(), json=generate_call)
        delete = await http.delete(image, headers=auth())
        unauthenticated = await http.get(image)

    assert fetches == [200, 200, 200] and delete.status_code == 200
    assert (generate.status_code, over.status_code) == (200, 429)
    assert unauthenticated.status_code == 401  # 不是公开地址，照旧要密钥
    assert gateway.received["image"] == 5
    stats = daily_report.collect_day_stats(str(tmp_path / "usage.sqlite3"), datetime.now(daily_report.BEIJING).date())
    assert stats.services["image"].calls == 1


async def test_services_without_a_limit_are_not_capped(tmp_path):
    gateway = App(make_settings(tmp_path, "search=1"))
    async with gateway as http:
        statuses = [(await http.post("/context7/mcp", headers=auth(), json=TOOL_CALL)).status_code for _ in range(5)]
    assert statuses == [200] * 5


async def test_count_survives_restart_and_limited_requests_do_not_count(tmp_path):
    settings = make_settings(tmp_path, "search=3")
    async with App(settings) as http:
        for _ in range(2):
            assert (await http.post("/search/search", headers=auth(), json={})).status_code == 200
    async with App(settings) as http:
        assert (await http.post("/search/search", headers=auth(), json={})).status_code == 200
        assert (await http.post("/search/search", headers=auth(), json={})).status_code == 429
    # 再重启：被拦下的那条不算调用，已用仍是 3，继续拦
    async with App(settings) as http:
        assert (await http.post("/search/search", headers=auth(), json={})).status_code == 429


async def test_limit_resets_at_beijing_midnight(tmp_path):
    limiter = DailyLimiter({"search": 1}, str(tmp_path / "none.sqlite3"), 0.8)
    before_midnight = datetime(2026, 11, 1, 15, 59, 59, tzinfo=UTC)  # 北京 11-01 23:59:59
    after_midnight = datetime(2026, 11, 1, 16, 0, 0, tzinfo=UTC)  # 北京 11-02 00:00

    assert (await limiter.admit("search", before_midnight)).allowed
    denied = await limiter.admit("search", before_midnight)
    assert not denied.allowed and denied.retry_after == 1
    assert (await limiter.admit("search", after_midnight)).allowed


def test_seconds_until_reset_uses_beijing_midnight():
    assert _seconds_until_reset(datetime(2026, 11, 1, 15, 59, tzinfo=UTC)) == 60
    assert _seconds_until_reset(datetime(2026, 11, 1, 16, 0, tzinfo=UTC)) == 86400


async def test_alerts_once_at_ratio_and_once_when_exhausted(tmp_path):
    sent: list[str] = []

    async def alert(text: str) -> None:
        sent.append(text)

    limiter = DailyLimiter({"search": 5}, str(tmp_path / "none.sqlite3"), 0.8, alert)
    results = []
    for _ in range(8):
        results.append((await limiter.admit("search")).allowed)
        await asyncio.sleep(0)

    assert results == [True] * 5 + [False] * 3
    assert len(sent) == 2
    assert "search 今日已调用 4 次" in sent[0] and "80%" in sent[0]
    assert "已达每日上限 5 次" in sent[1]


async def test_thresholds_crossed_before_restart_are_not_alerted_again(tmp_path):
    db = str(tmp_path / "usage.sqlite3")
    store = UsageStore(db, retention_days=3650)
    now = datetime.now(UTC).isoformat()
    for _ in range(4):
        _record(store, now, "search")
    _record(store, now, "search", outcome="limited", status=429)
    _record(store, now, "image")
    store.close()
    sent: list[str] = []

    async def alert(text: str) -> None:
        sent.append(text)

    limiter = DailyLimiter({"search": 5, "image": 1}, db, 0.8, alert)
    assert (await limiter.admit("search")).allowed  # 第 5 次：预警重启前已越过
    assert not (await limiter.admit("search")).allowed  # 用满：重启前已拦过
    assert not (await limiter.admit("image")).allowed  # image 用满但还没拦过：要告警
    await asyncio.sleep(0)

    assert sent == [
        "【agent-tools 网关】image 今日调用已达每日上限 1 次，之后的调用返回 429"
        "（daily_limit_exceeded），北京时间 0 点重置。上限在网关 .env 的 SERVICE_DAILY_LIMITS。"
    ]


async def test_unreadable_usage_db_fails_open(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="gateway")
    # 用目录冒充数据库文件：只读打开会失败
    limiter = DailyLimiter({"search": 1}, str(tmp_path), 0.8)

    assert (await limiter.admit("search")).allowed
    assert "daily_limit_load_failed" in caplog.text


async def test_gateway_pushes_limit_alert_to_feishu(tmp_path, monkeypatch):
    posted: list[dict] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url: str, json: dict):
            posted.append({"url": url, "json": json})
            return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(feishu, "_feishu_client", FakeClient)
    settings = make_settings(
        tmp_path,
        "search=1",
        limit_alert_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/patrol-token",
        limit_alert_sign_secret="patrol-secret",
    )
    async with App(settings) as http:
        await http.post("/search/search", headers=auth(), json={})
        await http.post("/search/search", headers=auth(), json={})
        await asyncio.sleep(0.05)

    texts = [p["json"]["content"]["text"] for p in posted]
    assert len(texts) == 2
    assert "达到每日上限 1 的 80%" in texts[0]
    assert "已达每日上限 1 次" in texts[1]
    assert all(p["json"]["sign"] for p in posted)
    assert "patrol-token" not in repr(settings)


def test_report_separates_limited_requests_and_notes_limits(tmp_path):
    db = str(tmp_path / "usage.sqlite3")
    store = UsageStore(db, retention_days=3650)
    for _ in range(3):
        _record(store, "2026-10-31T03:00:00+00:00", "search")
    _record(store, "2026-10-31T03:00:00+00:00", "search", status=502)
    for _ in range(2):
        _record(store, "2026-10-31T04:00:00+00:00", "search", outcome="limited", status=429)
    _record(store, "2026-10-31T04:00:00+00:00", "image")
    store.close()

    stats = daily_report.collect_day_stats(db, date(2026, 10, 31))
    text = daily_report.render_report(stats, None, None, {"search": 4, "image": 200})

    assert (stats.calls, stats.failed, stats.limited) == (5, 1, 2)
    assert dict(stats.products) == {"fusion": 5}
    lines = text.splitlines()
    assert lines[1] == "调用 5 次，失败 1 次，被每日上限拦下 2 次"
    assert "· search 4 / 1，上限 4，拦下 2 次" in lines
    assert "· image 1 / 0" in lines  # 用量没过半，不提上限


def test_default_limits_cover_paid_services_only():
    limits = Settings(_env_file=None).daily_limits()
    assert set(limits) <= set(SERVICES)
    # 免费服务不设上限
    assert "trends" not in limits and "context7" not in limits
