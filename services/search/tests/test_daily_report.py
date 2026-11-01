from datetime import UTC, datetime

import pytest

from app.models import ProviderUsageResponse


class FakeRedis:
    def __init__(self):
        self.values: dict[str, int | str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    async def incr(self, key: str) -> int:
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, _key: str, _ttl: int) -> bool:
        return True

    async def set(self, key: str, value: str, *, ex: int | None = None):
        self.values[key] = value
        return True


@pytest.fixture
def report_env(monkeypatch):
    from app import budget, daily_report

    redis = FakeRedis()
    sent: list[dict] = []

    async def fake_get_redis():
        return redis

    async def fake_send(text: str, *, webhook=None, secret=None) -> bool:
        sent.append({"text": text, "webhook": webhook, "secret": secret})
        return True

    async def fake_firecrawl_usage():
        return ProviderUsageResponse(
            provider="firecrawl",
            available=True,
            remaining_credits=69797,
            billing_period_end="2026-11-22T21:35:09.173Z",
        )

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)
    monkeypatch.setattr(daily_report, "get_redis", fake_get_redis)
    monkeypatch.setattr(daily_report, "send_feishu_text", fake_send)
    monkeypatch.setattr(daily_report, "get_firecrawl_usage", fake_firecrawl_usage)
    monkeypatch.setattr(
        daily_report,
        "list_providers",
        lambda: [{"name": n, "available": True} for n in ("tavily", "firecrawl", "deepseek")],
    )
    monkeypatch.setattr(daily_report.settings, "SEARCH_PROVIDER", "deepseek")
    monkeypatch.setattr(daily_report.settings, "SEARCH_FALLBACK_PROVIDERS", "firecrawl,tavily")
    monkeypatch.setattr(
        daily_report.settings, "PROVIDER_DAILY_LIMITS", "brave=100,tavily=100,firecrawl=500,deepseek=1000"
    )
    monkeypatch.setattr(daily_report.settings, "DAILY_REPORT_HOUR", 9)
    monkeypatch.setattr(daily_report.settings, "DAILY_REPORT_WEBHOOK", "https://open.feishu.cn/hook/aicost")
    monkeypatch.setattr(daily_report.settings, "DAILY_REPORT_SIGN_SECRET", "aicost-secret")
    return daily_report, redis, sent


def _seed_day(redis: FakeRedis, day: str = "2026-10-31") -> None:
    redis.values[f"budget:deepseek:calls:day:{day}"] = 312
    redis.values[f"budget:firecrawl:calls:day:{day}"] = 12
    redis.values[f"budget:tavily:rejected:day:{day}"] = 2
    redis.values[f"stats:fallback:error:day:{day}"] = 2
    redis.values[f"stats:fallback:insufficient:day:{day}"] = 8


def test_due_report_day_waits_until_configured_beijing_hour(report_env):
    daily_report, _redis, _sent = report_env

    assert daily_report.due_report_day(datetime(2026, 11, 1, 0, 59, tzinfo=UTC)) is None  # 北京 08:59
    assert daily_report.due_report_day(datetime(2026, 11, 1, 1, 0, tzinfo=UTC)) == "2026-10-31"  # 北京 09:00
    assert daily_report.due_report_day(datetime(2026, 11, 1, 15, 59, tzinfo=UTC)) == "2026-10-31"  # 北京 23:59


async def test_build_report_lists_providers_fallbacks_and_firecrawl_credits(report_env):
    daily_report, redis, _sent = report_env
    _seed_day(redis)

    text = await daily_report.build_report("2026-10-31")

    assert text == "\n".join(
        [
            "【搜索日报】10-31（北京时间）",
            "上游调用 / 每日上限：",
            "· deepseek（主） 312 / 1000",
            "· firecrawl（备用） 12 / 500",
            "· tavily（备用） 0 / 100，被额度拦下 2 次",
            "转备用 10 次（主 provider 报错 2，结果不足 8）",
            # 计费周期结束 11-22 21:35 UTC = 北京 11-23
            "Firecrawl 剩余 69,797 credits（计费周期至 11-23）",
        ]
    )
    # brave 只在额度配置里、没注册也没用量，不出现在日报里
    assert "brave" not in text


async def test_build_report_returns_none_when_day_has_no_counts(report_env):
    daily_report, _redis, _sent = report_env

    assert await daily_report.build_report("2026-10-31") is None


async def test_build_report_survives_firecrawl_usage_failure(monkeypatch, report_env):
    daily_report, redis, _sent = report_env
    _seed_day(redis)

    async def broken_usage():
        raise TimeoutError()

    monkeypatch.setattr(daily_report, "get_firecrawl_usage", broken_usage)

    text = await daily_report.build_report("2026-10-31")

    assert text.endswith("Firecrawl 剩余 credits：查询失败")


async def test_run_due_report_sends_once_per_day_to_report_webhook(report_env):
    daily_report, redis, sent = report_env
    _seed_day(redis)

    assert await daily_report.run_due_report(datetime(2026, 11, 1, 0, 30, tzinfo=UTC)) is None  # 北京 08:30 未到点
    assert await daily_report.run_due_report(datetime(2026, 11, 1, 1, 5, tzinfo=UTC)) == "sent"
    assert await daily_report.run_due_report(datetime(2026, 11, 1, 1, 10, tzinfo=UTC)) is None

    assert len(sent) == 1
    assert sent[0]["webhook"] == "https://open.feishu.cn/hook/aicost"
    assert sent[0]["secret"] == "aicost-secret"
    assert sent[0]["text"].startswith("【搜索日报】10-31")


async def test_run_due_report_marks_no_data_day_without_sending(report_env):
    daily_report, redis, sent = report_env

    assert await daily_report.run_due_report(datetime(2026, 11, 1, 1, 5, tzinfo=UTC)) == "no_data"
    assert await daily_report.run_due_report(datetime(2026, 11, 1, 1, 10, tzinfo=UTC)) is None
    assert sent == []
    assert redis.values["report:daily:sent:2026-10-31"] == "no_data"


async def test_run_due_report_retries_failed_send_at_most_three_times(monkeypatch, report_env):
    daily_report, redis, _sent = report_env
    _seed_day(redis)
    calls: list[str] = []

    async def rejected_send(text: str, *, webhook=None, secret=None) -> bool:
        calls.append(text)
        return False

    monkeypatch.setattr(daily_report, "send_feishu_text", rejected_send)
    now = datetime(2026, 11, 1, 1, 5, tzinfo=UTC)

    results = [await daily_report.run_due_report(now) for _ in range(5)]

    assert results == ["failed", "failed", "failed", None, None]
    assert len(calls) == 3
    assert "report:daily:sent:2026-10-31" not in redis.values


async def test_manual_test_send_prefixes_title_and_does_not_mark_day_sent(report_env):
    daily_report, redis, sent = report_env
    _seed_day(redis)

    assert await daily_report.send_daily_report("2026-10-31", test=True) == "sent"

    assert sent[0]["text"].startswith("【测试】【搜索日报】10-31")
    assert "report:daily:sent:2026-10-31" not in redis.values


async def test_send_feishu_text_uses_explicit_webhook_over_alert_webhook(monkeypatch):
    import httpx

    from app import budget

    posted: list[str] = []

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url: str, json: dict):
            posted.append(url)
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(budget.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(budget.settings, "BUDGET_ALERT_WEBHOOK", "https://open.feishu.cn/hook/patrol")

    assert await budget.send_feishu_text("x", webhook="https://open.feishu.cn/hook/aicost", secret="") is True
    assert await budget.send_feishu_text("x") is True
    assert posted == ["https://open.feishu.cn/hook/aicost", "https://open.feishu.cn/hook/patrol"]


async def test_record_fallback_counts_by_reason(monkeypatch):
    from app import budget

    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(budget, "get_redis", fake_get_redis)
    monkeypatch.setattr(budget, "budget_day", lambda now=None: "2026-11-01")

    await budget.record_fallback("error")
    await budget.record_fallback("insufficient")
    await budget.record_fallback("insufficient")

    assert await budget.get_fallback_counts("2026-11-01") == {"error": 1, "insufficient": 2}
