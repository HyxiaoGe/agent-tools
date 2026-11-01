"""付费 provider 的每日调用额度。

每次真正请求上游前先在 Redis 里占一个名额，超额直接拒绝，不再请求上游。
路由主调用、fallback、MCP 工具都经 registry 拿到包装后的 provider，因此都受同一份额度约束。
"""

import asyncio
import base64
import hashlib
import hmac
import math
import time
from datetime import UTC, datetime, timedelta, timezone

import httpx

from app.cache import get_redis
from app.config import parse_daily_limits, settings
from app.logger import get_logger
from app.models import ProviderBudgetStatus, SearchRequest, SearchResponse

log = get_logger()

# 额度按北京时间自然日重置；中国没有夏令时，固定 +8 即可，不依赖容器里的 tzdata。
BEIJING = timezone(timedelta(hours=8))
_KEY_TTL_SECONDS = 3 * 86400
# 后台告警任务的强引用，防止 create_task 出来的任务被 GC 提前回收。
_pending_alerts: set[asyncio.Task] = set()


class ProviderBudgetExceeded(Exception):
    def __init__(self, provider: str, limit: int, day: str) -> None:
        super().__init__(f"{provider} 今日调用额度已用完（{limit} 次/天，北京时间 {day}）")
        self.provider = provider
        self.limit = limit
        self.day = day


def budget_day(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    return value.astimezone(BEIJING).date().isoformat()


def _calls_key(provider: str, day: str) -> str:
    return f"budget:{provider}:calls:day:{day}"


def _rejected_key(provider: str, day: str) -> str:
    return f"budget:{provider}:rejected:day:{day}"


def _warn_threshold(limit: int) -> int | None:
    if limit <= 0:
        return None
    return max(1, math.ceil(limit * settings.BUDGET_ALERT_RATIO))


async def reserve_provider_call(provider: str) -> None:
    """占用当日一个名额；超额抛 ProviderBudgetExceeded。Redis 不可用时放行，额度闸不能把搜索本身拖死。"""
    limit = parse_daily_limits(settings.PROVIDER_DAILY_LIMITS).get(provider)
    if limit is None:
        return

    day = budget_day()
    calls_key = _calls_key(provider, day)
    try:
        redis = await get_redis()
        used = await redis.incr(calls_key)
        if used == 1:
            await redis.expire(calls_key, _KEY_TTL_SECONDS)
        if used > limit:
            # 被拒的不算真实调用，计数退回，只记拒绝次数。
            await redis.decr(calls_key)
            rejected_key = _rejected_key(provider, day)
            if await redis.incr(rejected_key) == 1:
                await redis.expire(rejected_key, _KEY_TTL_SECONDS)
    except Exception as exc:
        log.warning("budget_check_failed", provider=provider, error=str(exc))
        return

    if used > limit:
        log.warning("provider_budget_exceeded", provider=provider, limit=limit, day=day)
        _schedule_alert(
            provider,
            "exhausted",
            day,
            f"【搜索额度】{provider} 今日调用已达上限 {limit} 次（北京时间 {day}），之后的请求会跳过 {provider}。"
            "如需临时放宽，改 search 服务 .env 的 PROVIDER_DAILY_LIMITS 后重建容器。",
        )
        raise ProviderBudgetExceeded(provider, limit, day)

    if used == _warn_threshold(limit) and used < limit:
        _schedule_alert(
            provider,
            "warn",
            day,
            f"【搜索额度】{provider} 今日已用 {used}/{limit} 次（北京时间 {day}），接近上限。"
            "如果正在跑批量实验，请先停下确认费用。",
        )


def _schedule_alert(provider: str, kind: str, day: str, text: str) -> None:
    task = asyncio.create_task(_alert_once(provider, kind, day, text))
    _pending_alerts.add(task)
    task.add_done_callback(_pending_alerts.discard)


async def _alert_once(provider: str, kind: str, day: str, text: str) -> None:
    try:
        redis = await get_redis()
        first = await redis.set(f"budget:{provider}:alerted:{kind}:{day}", "1", ex=_KEY_TTL_SECONDS, nx=True)
        if not first:
            return
        log.warning("provider_budget_alert", provider=provider, kind=kind, day=day)
        await send_feishu_text(text)
    except Exception as exc:
        log.warning("provider_budget_alert_failed", provider=provider, kind=kind, error=str(exc))


async def send_feishu_text(text: str, *, webhook: str | None = None, secret: str | None = None) -> bool:
    """发飞书自定义机器人文本消息，飞书返回 code 0 时为 True。默认发到额度告警群（巡检群）。"""
    url = (settings.BUDGET_ALERT_WEBHOOK if webhook is None else webhook).strip()
    if not url:
        return False
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    secret = (settings.BUDGET_ALERT_SIGN_SECRET if secret is None else secret).strip()
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}".encode()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()).decode()
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    if body.get("code", 0) != 0:
        log.warning("feishu_alert_rejected", code=body.get("code"), msg=body.get("msg"))
        return False
    return True


# 转备用的次数，按触发原因分开计：主 provider 报错 / 结果不足。只用于日报统计，不参与额度判断。
FALLBACK_REASONS = ("error", "insufficient")


def _fallback_key(reason: str, day: str) -> str:
    return f"stats:fallback:{reason}:day:{day}"


async def record_fallback(reason: str) -> None:
    key = _fallback_key(reason, budget_day())
    try:
        redis = await get_redis()
        if await redis.incr(key) == 1:
            await redis.expire(key, _KEY_TTL_SECONDS)
    except Exception as exc:
        log.warning("fallback_stat_failed", reason=reason, error=str(exc))


async def get_fallback_counts(day: str) -> dict[str, int]:
    redis = await get_redis()
    values = await redis.mget([_fallback_key(reason, day) for reason in FALLBACK_REASONS])
    return {reason: int(value or 0) for reason, value in zip(FALLBACK_REASONS, values, strict=True)}


async def get_budget_status(day: str | None = None) -> list[ProviderBudgetStatus]:
    limits = parse_daily_limits(settings.PROVIDER_DAILY_LIMITS)
    day = day or budget_day()
    names = sorted(limits)
    used_values: list = [None] * len(names)
    rejected_values: list = [None] * len(names)
    available = True
    try:
        redis = await get_redis()
        if names:
            used_values = await redis.mget([_calls_key(name, day) for name in names])
            rejected_values = await redis.mget([_rejected_key(name, day) for name in names])
    except Exception as exc:
        available = False
        log.warning("budget_status_failed", error=str(exc))
    return [
        ProviderBudgetStatus(
            provider=name,
            day=day,
            limit=limits[name],
            used=int(used or 0),
            rejected=int(rejected or 0),
            available=available,
        )
        for name, used, rejected in zip(names, used_values, rejected_values, strict=True)
    ]


class BudgetGuardedProvider:
    """真正调用上游前先占额度；其他属性透传给原 provider。"""

    def __init__(self, name: str, inner) -> None:
        self._name = name
        self._inner = inner

    async def search(self, request: SearchRequest) -> SearchResponse:
        await reserve_provider_call(self._name)
        return await self._inner.search(request)

    def __getattr__(self, item: str):
        return getattr(self._inner, item)
