"""上游请求的公共部分：错误分类、额度用尽的记忆、每家的并发上限、每天的计数。

错误分几类（UpstreamError.kind），决定要不要转给另一家：
- quota：额度用尽（高德 10003、10044 等，腾讯 121）。记下来，到恢复时间前跳过这一家的这组接口。
- rate：超出每秒上限；timeout、unavailable：超时、连不上、5xx、服务商自己出错；
  invalid_response：返回的不是能用的 JSON；auth：密钥无效、没开权限（同时记错误日志，这是配置问题）。
- unsupported：这一家做不了这次请求（比如高德算公交要两端城市），没发请求。
- rejected：上游拒了这次请求本身（参数不对、算不出路线、地址解析没结果）。不转：换一家多半也不行，还白占额度。
查到 0 条不是错误，也不转。

额度记忆在内存里，按“服务商 + 接口组”记，跳到 min(恢复时间, 1 小时后)：1 小时后再试一次，
因为腾讯在控制台调配额后会恢复（11-04 实测同一把 key 的 121 一小时内在接口间变过）。
重启就忘，再碰一次只多一个失败的请求。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

logger = logging.getLogger("maps")

BEIJING = ZoneInfo("Asia/Shanghai")
FALLBACK_KINDS = frozenset({"quota", "rate", "auth", "unavailable", "timeout", "invalid_response", "unsupported"})
RECHECK_AFTER = timedelta(hours=1)
RETENTION_DAYS = 3
COUNTERS = ("requests", "failed", "quota", "skipped")

T = TypeVar("T")


class UpstreamError(Exception):
    def __init__(self, kind: str, provider: str, api: str, code: str = "", *, reset_at: datetime | None = None):
        super().__init__(f"{provider} {api}: {kind} {code}".strip())
        self.kind = kind
        self.provider = provider
        self.api = api
        self.code = code
        self.reset_at = reset_at


def beijing_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(BEIJING).date()


class QuotaMemo:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._clock = clock
        self._until: dict[tuple[str, str], tuple[datetime, datetime]] = {}

    def blocked(self, provider: str, group: str) -> datetime | None:
        """在跳过期内返回恢复时间，否则 None。"""
        entry = self._until.get((provider, group))
        if entry is None:
            return None
        skip_until, reset_at = entry
        if self._clock() >= skip_until:
            del self._until[(provider, group)]
            return None
        return reset_at

    def mark(self, provider: str, group: str, reset_at: datetime) -> None:
        now = self._clock()
        self._until[(provider, group)] = (min(reset_at, now + RECHECK_AFTER), reset_at)
        logger.warning("maps_quota_exhausted provider=%s group=%s reset_at=%s", provider, group, reset_at.isoformat())


class Usage:
    """每天（北京时间）各家发了多少请求、失败多少、额度用尽多少、因为记着用尽跳过多少，以及转给下一家几次。
    只有计数，没有地名。状态文件读不了时从 0 计，只记日志。"""

    def __init__(self, state_path: str = "", clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._path = Path(state_path) if state_path else None
        self._clock = clock
        self._state: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("maps_usage_state_unreadable: %s", type(exc).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def _day(self) -> dict:
        today = beijing_today(self._clock())
        return self._state.setdefault(today.isoformat(), {})

    def count(self, provider: str, counter: str) -> None:
        day = self._day()
        counters = day.setdefault(provider, {})
        counters[counter] = int(counters.get(counter, 0)) + 1
        self._save()

    def fallback(self) -> None:
        day = self._day()
        day["fallbacks"] = int(day.get("fallbacks", 0)) + 1
        self._save()

    def summary(self, day: date) -> dict:
        entry = self._state.get(day.isoformat(), {})
        providers = []
        for name, counters in sorted(entry.items()):
            if not isinstance(counters, dict):
                continue
            providers.append({"provider": name, **{c: int(counters.get(c, 0)) for c in COUNTERS}})
        return {
            "day": day.isoformat(),
            "available": True,
            "providers": providers,
            "fallbacks": int(entry.get("fallbacks", 0)),
        }

    def _save(self) -> None:
        if self._path is None:
            return
        cutoff = (beijing_today(self._clock()) - timedelta(days=RETENTION_DAYS)).isoformat()
        self._state = {day: entry for day, entry in self._state.items() if day > cutoff}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".usage.")
            with os.fdopen(fd, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("maps_usage_state_write_failed: %s", type(exc).__name__)


class Gate:
    """一家上游的闸：先看额度记忆，再按并发上限发请求，顺手计数。"""

    def __init__(self, provider: str, memo: QuotaMemo, usage: Usage, concurrency: int = 3):
        self.provider = provider
        self._memo = memo
        self._usage = usage
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def call(self, group: str, api: str, send: Callable[[], Awaitable[T]]) -> T:
        reset_at = self._memo.blocked(self.provider, group)
        if reset_at is not None:
            self._usage.count(self.provider, "skipped")
            raise UpstreamError("quota", self.provider, api, "remembered", reset_at=reset_at)
        async with self._semaphore:
            self._usage.count(self.provider, "requests")
            try:
                return await send()
            except UpstreamError as error:
                if error.kind == "quota" and error.reset_at is not None:
                    self._memo.mark(self.provider, group, error.reset_at)
                    self._usage.count(self.provider, "quota")
                elif error.kind in FALLBACK_KINDS:
                    self._usage.count(self.provider, "failed")
                if error.kind == "auth":
                    logger.error("maps_auth_failed provider=%s api=%s code=%s", self.provider, api, error.code)
                raise
