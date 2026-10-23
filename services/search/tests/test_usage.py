from datetime import UTC, datetime


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def incrby(self, key: str, amount: int):
        self.ops.append(("incrby", key, amount))
        return self

    def incr(self, key: str):
        self.ops.append(("incr", key))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == "incrby":
                _, key, amount = op
                self.redis.values[key] = int(self.redis.values.get(key, 0)) + amount
            else:
                _, key = op
                self.redis.values[key] = int(self.redis.values.get(key, 0)) + 1


class FakeRedis:
    def __init__(self, values: dict[str, int | str] | None = None):
        self.values = values or {}

    def pipeline(self):
        return FakePipeline(self)

    async def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    async def get(self, key: str):
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.values[key] = value
        self.values[f"{key}:ttl"] = ttl

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.values[f"{key}:ttl"] = ex
        return True


async def test_record_search_credits_increments_daily_credit_and_request_keys(monkeypatch):
    from app import usage

    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(usage, "get_redis", fake_get_redis)
    monkeypatch.setattr(usage, "_utc_now", lambda: datetime(2026, 6, 25, 12, 0, tzinfo=UTC))

    await usage.record_search_credits("firecrawl", 7)

    assert redis.values == {
        "usage:firecrawl:credits:day:2026-06-25": 7,
        "usage:firecrawl:requests:day:2026-06-25": 1,
    }


async def test_get_recorded_provider_usage_sums_daily_keys_for_period(monkeypatch):
    from app import usage

    redis = FakeRedis(
        {
            "usage:firecrawl:credits:day:2026-06-22": "3",
            "usage:firecrawl:requests:day:2026-06-22": "1",
            "usage:firecrawl:credits:day:2026-06-23": "5",
            "usage:firecrawl:requests:day:2026-06-23": "2",
        }
    )

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(usage, "get_redis", fake_get_redis)

    summary = await usage.get_recorded_provider_usage(
        "firecrawl",
        period_start="2026-06-22T21:35:09.173Z",
        period_end="2026-06-24T21:35:09.173Z",
    )

    assert summary.model_dump() == {
        "provider": "firecrawl",
        "available": True,
        "credits_used": 8,
        "request_count": 3,
        "period_start": "2026-06-22T21:35:09.173Z",
        "period_end": "2026-06-24T21:35:09.173Z",
        "source": "search_response_credits_used",
        "daily": [
            {"date": "2026-06-22", "credits_used": 3, "request_count": 1},
            {"date": "2026-06-23", "credits_used": 5, "request_count": 2},
            {"date": "2026-06-24", "credits_used": 0, "request_count": 0},
        ],
    }


async def test_get_recorded_provider_usage_marks_unavailable_when_redis_fails(monkeypatch):
    from app import usage

    async def fake_get_redis():
        raise RuntimeError("redis down")

    monkeypatch.setattr(usage, "get_redis", fake_get_redis)

    summary = await usage.get_recorded_provider_usage(
        "firecrawl",
        period_start="2026-06-22T21:35:09.173Z",
        period_end="2026-06-24T21:35:09.173Z",
    )

    assert summary.available is False
    assert summary.credits_used == 0
    assert summary.request_count == 0
    assert summary.daily == []
