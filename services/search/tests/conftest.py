import pytest

from app import cache


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    # 默认 REDIS_URL 是 localhost:6379，CI 机器上那是共享的 middleware-redis，测试不能读写它。
    # 指向一个拒绝连接的端口，需要 redis 的测试自己替换 get_redis。
    monkeypatch.setattr(cache.settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(cache, "_redis", None)


@pytest.fixture(autouse=True)
def fallback_stats(monkeypatch):
    # 路由转备用时会写 Redis 计数；测试里统一记到列表，需要断言的测试直接请求这个 fixture。
    from app.routes import search as search_route

    recorded: list[str] = []

    async def fake_record_fallback(reason: str) -> None:
        recorded.append(reason)

    monkeypatch.setattr(search_route, "record_fallback", fake_record_fallback)
    return recorded
