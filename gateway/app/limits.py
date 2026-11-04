"""按服务的每日调用上限（北京时间自然日），兜底防止批量实验或失控循环把付费上游打穿。

- 只算真正的调用：REST 请求和 MCP tools/call；MCP 握手（initialize、tools/list、SSE）、
  按 id 取删 image-service 的图（/image/v1/images/...）不算也不拦（见 proxy.counts_as_call）。
- 计数在内存里（网关单进程）；每天第一次用到时从用量记录补回当天已有的调用数，重启不清零。
  被拦下的请求以 outcome=limited 记账，不计入调用数。
- 用到 alert_ratio 和用满时各推一次飞书巡检群（每个服务每天各一次）。
服务内部还有各自按上游的额度（search 的 PROVIDER_DAILY_LIMITS 等），这里是最外层。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.daily_report import BEIJING, collect_day_stats

logger = logging.getLogger("gateway")

Alert = Callable[[str], Coroutine[Any, Any, object]]


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    limit: int | None = None
    used: int = 0
    # 距北京时间下一个 0 点的秒数，拒绝时作为 Retry-After
    retry_after: int = 0


def _seconds_until_reset(now: datetime) -> int:
    local = now.astimezone(BEIJING)
    midnight = datetime(local.year, local.month, local.day, tzinfo=BEIJING) + timedelta(days=1)
    return max(1, math.ceil((midnight - local).total_seconds()))


class DailyLimiter:
    def __init__(self, limits: Mapping[str, int], db_path: str, alert_ratio: float, alert: Alert | None = None):
        self._limits = dict(limits)
        self._db_path = db_path
        self._ratio = alert_ratio
        self._alert = alert
        self._day: date | None = None
        self._used: Counter = Counter()
        self._alerted: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def _warn_at(self, limit: int) -> int:
        return max(1, math.ceil(limit * self._ratio))

    async def _roll_over(self, today: date) -> None:
        async with self._lock:
            if self._day == today:
                return
            used: Counter = Counter()
            alerted: set[tuple[str, str]] = set()
            try:
                stats = await asyncio.to_thread(collect_day_stats, self._db_path, today)
            except Exception as exc:
                # 读不了用量记录时从 0 开始计，不因此拦下请求；服务自己的上游额度仍然有效。
                logger.warning("daily_limit_load_failed: %s", type(exc).__name__)
            else:
                for service, limit in self._limits.items():
                    svc = stats.services.get(service)
                    if svc is None:
                        continue
                    used[service] = svc.calls
                    # 重启前已经越过阈值的，视为已告警过，不重复推送。
                    if limit and svc.calls >= self._warn_at(limit):
                        alerted.add((service, "warning"))
                    if svc.limited:
                        alerted.add((service, "exhausted"))
            self._used, self._alerted, self._day = used, alerted, today

    async def admit(self, service: str, now: datetime | None = None) -> LimitDecision:
        limit = self._limits.get(service)
        if limit is None:
            return LimitDecision(allowed=True)
        now = now or datetime.now(UTC)
        today = now.astimezone(BEIJING).date()
        if self._day != today:
            await self._roll_over(today)
        used = self._used[service]
        if used >= limit:
            self._notify(
                service,
                "exhausted",
                f"【agent-tools 网关】{service} 今日调用已达每日上限 {limit:,} 次，之后的调用返回 429"
                "（daily_limit_exceeded），北京时间 0 点重置。上限在网关 .env 的 SERVICE_DAILY_LIMITS。",
            )
            return LimitDecision(False, limit, used, _seconds_until_reset(now))
        used += 1
        self._used[service] = used
        if used >= self._warn_at(limit):
            self._notify(
                service,
                "warning",
                f"【agent-tools 网关】{service} 今日已调用 {used:,} 次，达到每日上限 {limit:,} 的 "
                f"{self._ratio:.0%}。用满后该服务返回 429，北京时间 0 点重置。",
            )
        return LimitDecision(True, limit, used, _seconds_until_reset(now))

    def _notify(self, service: str, kind: str, text: str) -> None:
        key = (service, kind)
        if key in self._alerted:
            return
        self._alerted.add(key)
        logger.warning("service_daily_limit_%s: %s", kind, service)
        if self._alert is None:
            return
        # 告警不阻塞请求；任务引用留在集合里，防止被垃圾回收。
        task = asyncio.create_task(self._alert(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
