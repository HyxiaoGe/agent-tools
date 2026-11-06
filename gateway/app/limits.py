"""每日调用上限（北京时间自然日），兜底防止批量实验或失控循环把付费上游打穿。

- 两层同时生效：按服务的总上限（SERVICE_DAILY_LIMITS，所有产品合计），和按产品的上限
  （PRODUCTS[*].daily_limits，几个产品共用一份付费额度时防止一个产品把全天额度用光）。
- 只算真正的调用：REST 请求和 MCP tools/call；MCP 握手（initialize、tools/list、SSE）、
  上传、按 id 取删 image-service 的图（/image/v1/images、/image/v1/images/...）不算也不拦（见 proxy.counts_as_call）。
- 转发前先占一次；转发结束后不占上限的（网关连接池满没转出去、服务声明没调上游）退回去，
  口径见 proxy.consumes_quota。被拦下的请求以 outcome=limited 记账，也不占。
- 计数在内存里（网关单进程）；每天第一次用到时从用量记录补回当天已占的次数，重启不清零。
- 用到 alert_ratio 和用满时各推一次飞书巡检群（每个服务、每个产品的上限每天各一次）。
服务内部还有各自按上游的额度（search 的 PROVIDER_DAILY_LIMITS 等），这里是最外层。
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.daily_report import BEIJING, day_bounds_utc
from app.proxy import consumes_quota, counts_as_call

logger = logging.getLogger("gateway")

Alert = Callable[[str], Coroutine[Any, Any, object]]


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    limit: int | None = None
    used: int = 0
    # 距北京时间下一个 0 点的秒数，拒绝时作为 Retry-After
    retry_after: int = 0
    # 拒绝时是哪一层的上限：service（服务总上限）或 product（该产品的上限）
    scope: str | None = None
    # 放行时占了计数的那一天；转发结束后退回时只退同一天的
    day: date | None = None


@dataclass
class QuotaUsage:
    """当天已占上限的次数和被拦下的次数，键是 (服务, 产品)。"""

    used: Counter
    limited: Counter

    def service_total(self, counter: Counter, service: str) -> int:
        return sum(count for (svc, _app), count in counter.items() if svc == service)


def collect_quota_usage(db_path: str, day: date) -> QuotaUsage:
    start, end = day_bounds_utc(day)
    usage = QuotaUsage(Counter(), Counter())
    if not Path(db_path).exists():
        return usage
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT service, app_id, path, tool, outcome, COUNT(*)
            FROM requests WHERE ts >= ? AND ts < ?
            GROUP BY service, app_id, path, tool, outcome
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    for service, app_id, path, tool, outcome, count in rows:
        if not counts_as_call(service, path, tool):
            continue
        if outcome == "limited":
            usage.limited[(service, app_id)] += count
        elif consumes_quota(outcome):
            usage.used[(service, app_id)] += count
    return usage


def _seconds_until_reset(now: datetime) -> int:
    local = now.astimezone(BEIJING)
    midnight = datetime(local.year, local.month, local.day, tzinfo=BEIJING) + timedelta(days=1)
    return max(1, math.ceil((midnight - local).total_seconds()))


class DailyLimiter:
    def __init__(
        self,
        limits: Mapping[str, int],
        db_path: str,
        alert_ratio: float,
        alert: Alert | None = None,
        *,
        product_limits: Mapping[str, Mapping[str, int]] | None = None,
    ):
        self._limits = dict(limits)
        self._product_limits = {app: dict(svc) for app, svc in (product_limits or {}).items()}
        self._db_path = db_path
        self._ratio = alert_ratio
        self._alert = alert
        self._day: date | None = None
        self._used: Counter = Counter()  # 服务 → 所有产品合计
        self._product_used: Counter = Counter()  # (服务, 产品)
        # 已告警过的：(服务, 类型) 是服务总上限，(服务, 产品, 类型) 是产品上限
        self._alerted: set[tuple[str, ...]] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def _warn_at(self, limit: int) -> int:
        return max(1, math.ceil(limit * self._ratio))

    def _product_limit(self, service: str, app_id: str) -> int | None:
        return self._product_limits.get(app_id, {}).get(service)

    async def _roll_over(self, today: date) -> None:
        async with self._lock:
            if self._day == today:
                return
            used: Counter = Counter()
            product_used: Counter = Counter()
            alerted: set[tuple[str, ...]] = set()
            try:
                usage = await asyncio.to_thread(collect_quota_usage, self._db_path, today)
            except Exception as exc:
                # 读不了用量记录时从 0 开始计，不因此拦下请求；服务自己的上游额度仍然有效。
                logger.warning("daily_limit_load_failed: %s", type(exc).__name__)
            else:
                product_used.update(usage.used)
                for (service, _app), count in usage.used.items():
                    used[service] += count
                # 重启前已经越过阈值的，视为已告警过，不重复推送；已经拦过的，视为已推过用满。
                # 用量记录分不出当时拦下是哪一层的上限，服务这层按有没有拦过算，可能少推一次用满。
                for service, limit in self._limits.items():
                    if used[service] >= self._warn_at(limit):
                        alerted.add((service, "warning"))
                    if usage.service_total(usage.limited, service):
                        alerted.add((service, "exhausted"))
                for app_id, limits in self._product_limits.items():
                    for service, limit in limits.items():
                        if product_used[(service, app_id)] >= self._warn_at(limit):
                            alerted.add((service, app_id, "warning"))
                        if usage.limited[(service, app_id)]:
                            alerted.add((service, app_id, "exhausted"))
            self._used, self._product_used, self._alerted, self._day = used, product_used, alerted, today

    async def admit(self, service: str, app_id: str, now: datetime | None = None) -> LimitDecision:
        limit = self._limits.get(service)
        product_limit = self._product_limit(service, app_id)
        if limit is None and product_limit is None:
            return LimitDecision(allowed=True)
        now = now or datetime.now(UTC)
        today = now.astimezone(BEIJING).date()
        if self._day != today:
            await self._roll_over(today)
        retry_after = _seconds_until_reset(now)
        used = self._used[service]
        product_used = self._product_used[(service, app_id)]
        if limit is not None and used >= limit:
            self._notify(
                (service, "exhausted"),
                f"【agent-tools 网关】{service} 今日调用已达每日上限 {limit:,} 次，之后的调用返回 429"
                "（daily_limit_exceeded），北京时间 0 点重置。上限在网关 .env 的 SERVICE_DAILY_LIMITS。",
            )
            return LimitDecision(False, limit, used, retry_after, scope="service")
        if product_limit is not None and product_used >= product_limit:
            self._notify(
                (service, app_id, "exhausted"),
                f"【agent-tools 网关】{app_id} 今日调用 {service} 已达该产品的每日上限 {product_limit:,} 次，"
                f"之后 {app_id} 调 {service} 返回 429（daily_limit_exceeded），北京时间 0 点重置。"
                f"上限在网关 .env 的 PRODUCTS.{app_id}.daily_limits。",
            )
            return LimitDecision(False, product_limit, product_used, retry_after, scope="product")
        used += 1
        product_used += 1
        self._used[service] = used
        self._product_used[(service, app_id)] = product_used
        if limit is not None and used >= self._warn_at(limit):
            self._notify(
                (service, "warning"),
                f"【agent-tools 网关】{service} 今日已调用 {used:,} 次，达到每日上限 {limit:,} 的 "
                f"{self._ratio:.0%}。用满后该服务返回 429，北京时间 0 点重置。",
            )
        if product_limit is not None and product_used >= self._warn_at(product_limit):
            self._notify(
                (service, app_id, "warning"),
                f"【agent-tools 网关】{app_id} 今日已调用 {service} {product_used:,} 次，达到该产品每日上限 "
                f"{product_limit:,} 的 {self._ratio:.0%}。用满后 {app_id} 调 {service} 返回 429，北京时间 0 点重置。",
            )
        return LimitDecision(True, limit, used, retry_after, day=today)

    def release(self, service: str, app_id: str, decision: LimitDecision) -> None:
        """转发结束后发现这次不占上限（见 proxy.consumes_quota）时退回。只退放行那天的计数：
        跨过 0 点时新一天是从用量记录补回的，本来就没算这一次。预警已经推了的不撤回。"""
        if not decision.allowed or decision.day is None or decision.day != self._day:
            return
        self._used[service] = max(0, self._used[service] - 1)
        key = (service, app_id)
        self._product_used[key] = max(0, self._product_used[key] - 1)

    def _notify(self, key: tuple[str, ...], text: str) -> None:
        if key in self._alerted:
            return
        self._alerted.add(key)
        logger.warning("service_daily_limit_%s: %s", key[-1], "/".join(key[:-1]))
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
