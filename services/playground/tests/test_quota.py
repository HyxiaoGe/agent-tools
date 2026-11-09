import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.limits import LimitError
from app.main import create_app
from app.quota import QuotaCounter
from tests.test_playground import settings, upstream


def test_identity_and_counters_survive_restart_without_storing_ip(tmp_path):
    config = settings(quota_db_path=str(tmp_path / "counter.sqlite3"), requests_per_day=2)
    first = QuotaCounter(config)
    visitor = first.identity("203.0.113.25")
    first.reserve(visitor)
    second = QuotaCounter(config)
    assert second.identity("203.0.113.25") == visitor
    assert second.snapshot(visitor)["requests"]["remaining"] == 1
    assert b"203.0.113.25" not in (tmp_path / "counter.sqlite3").read_bytes()
    second.reserve(visitor)
    with pytest.raises(LimitError):
        first.reserve(visitor)
    assert first.snapshot(visitor)["requests"]["used"] == 2


def test_site_limit_is_atomic_across_visitors_and_instances(tmp_path):
    config = settings(quota_db_path=str(tmp_path / "atomic.sqlite3"), site_requests_per_day=5)
    counters = [QuotaCounter(config), QuotaCounter(config)]

    def attempt(index):
        try:
            counters[index % 2].reserve(f"visitor_{index}")
            return True
        except LimitError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(20)))
    assert sum(outcomes) == 5
    status = counters[0].snapshot("new_visitor")
    assert status["site_requests"] == {"limit": 5, "used": 5, "remaining": 0}
    assert status["can_run"] is False


def test_images_consume_both_counters_without_blocking_other_tools():
    counter = QuotaCounter(settings(images_per_day=1, site_images_per_day=1))
    counter.reserve("a", image=True)
    status = counter.snapshot("a")
    assert status["requests"]["used"] == 1
    assert status["images"]["remaining"] == 0
    assert status["can_run"] is True
    assert status["can_generate"] is False
    with pytest.raises(LimitError):
        counter.reserve("b", image=True)
    counter.reserve("b")
    assert counter.snapshot("b")["site_requests"]["used"] == 2


def test_reset_uses_shanghai_midnight_and_leaves_old_day_intact():
    clock = [datetime(2026, 11, 9, 15, 59, 59, tzinfo=UTC)]
    counter = QuotaCounter(settings(requests_per_day=1), clock=lambda: clock[0])
    counter.reserve("a")
    before = counter.snapshot("a")
    assert before["day"] == "2026-11-09"
    assert before["resets_at"] == "2026-11-10T00:00:00+08:00"
    clock[0] = datetime(2026, 11, 9, 16, 0, 0, tzinfo=UTC)
    assert counter.snapshot("a")["requests"]["remaining"] == 1
    counter.reserve("a")
    clock[0] = datetime(2026, 11, 9, 15, 59, 59, tzinfo=UTC)
    assert counter.snapshot("a")["requests"]["remaining"] == 0


def test_refresh_and_app_restart_cannot_reset_api_allowance():
    config = settings(requests_per_day=1)
    with TestClient(create_app(config, transport=httpx.MockTransport(upstream))) as client:
        before = client.get("/api/quota").json()
        assert before["requests"]["remaining"] == 1
        assert client.get("/api/quota").json()["requests"]["remaining"] == 1
        result = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert result.json()["quota"]["requests"]["remaining"] == 0
        assert client.get("/api/quota").json()["requests"]["remaining"] == 0

    def forbidden(request):
        pytest.fail("额度用完后不应再请求网关")

    with TestClient(create_app(config, transport=httpx.MockTransport(forbidden))) as restarted:
        limited = restarted.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert limited.status_code == 429
        assert limited.json()["quota"]["requests"]["remaining"] == 0
        assert int(limited.headers["retry-after"]) > 0


def test_site_limit_cannot_be_bypassed_by_changing_identity():
    config = settings(site_requests_per_day=1)
    with TestClient(create_app(config, transport=httpx.MockTransport(upstream)), client=("198.51.100.1", 1)) as first:
        first.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})

    def forbidden(request):
        pytest.fail("更换地址后仍应受到全站总额限制")

    with TestClient(create_app(config, transport=httpx.MockTransport(forbidden)), client=("198.51.100.2", 2)) as second:
        result = second.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert result.status_code == 429
        assert result.json()["quota"]["site_requests"]["remaining"] == 0


def test_invalid_input_is_free_but_submitted_failures_count():
    config = settings()

    def failure(request):
        if json.loads(request.content)["method"] != "tools/call":
            return upstream(request)
        raise httpx.ReadTimeout("模拟上游超时")

    with TestClient(create_app(config, transport=httpx.MockTransport(failure))) as client:
        client.post("/api/run", json={"tool": "search_web", "arguments": {"query": ""}})
        assert client.get("/api/quota").json()["requests"]["used"] == 0
        failed = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert failed.status_code == 504
        assert failed.json()["quota"]["requests"]["used"] == 1


def test_database_failure_rejects_calls_before_gateway(monkeypatch):
    def forbidden(request):
        pytest.fail("计数不可用时不应继续消耗共享额度")

    with TestClient(create_app(settings(), transport=httpx.MockTransport(forbidden))) as client:

        def fail(*args, **kwargs):
            import sqlite3

            raise sqlite3.OperationalError("模拟不可写")

        monkeypatch.setattr(QuotaCounter, "reserve", fail)
        result = client.post("/api/run", json={"tool": "search_web", "arguments": {"query": "Python"}})
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "quota_unavailable"
