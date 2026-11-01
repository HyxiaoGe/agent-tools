"""按上游的每日调用额度（北京时间自然日），兜底防止批量实验或失控循环把付费上游打穿。

- 上游：volcano（Seedream）、google（Gemini，经 LiteLLM）、alibaba（Qwen-Image）、tavily（参考图搜索）。
  参考图评审用的 LiteLLM 小模型不计。
- 每次真正请求上游前占一个名额，缓存命中不占；超额时生图按备用链换下一个模型，参考图搜索直接跳过。
- 计数在 Redis，所有进程共用；Redis 出错时不放行（与生成准入一致）。
- 用到 quota_alert_ratio 和用满时各推一次飞书巡检群。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anyio
import httpx
import structlog

from src.config import PROVIDER_NAMES, QUOTA_PROVIDERS, SUPPORTED_MODELS, Settings

logger = structlog.get_logger()
PREFIX = "quota:image:v1:"
BEIJING = ZoneInfo("Asia/Shanghai")
# 计数保留 3 天，够日报读前一天
COUNTER_TTL_SECONDS = 3 * 86400
REDIS_TIMEOUT_SECONDS = 0.5
_alert_tasks: set[asyncio.Task] = set()


class QuotaExceeded(Exception):
    def __init__(self, provider: str, limit: int):
        self.provider, self.limit = provider, limit
        super().__init__(f"{provider} daily quota {limit} used up")


class QuotaUnavailable(Exception):
    pass


def provider_for_model(model: str) -> str:
    return PROVIDER_NAMES[SUPPORTED_MODELS[model].provider]


def beijing_today(now: datetime | None = None) -> date:
    return (now or datetime.now(BEIJING)).astimezone(BEIJING).date()


def seconds_until_reset(now: datetime | None = None) -> int:
    local = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    midnight = datetime(local.year, local.month, local.day, tzinfo=BEIJING) + timedelta(days=1)
    return max(1, math.ceil((midnight - local).total_seconds()))


def _key(provider: str, kind: str, day: date) -> str:
    return f"{PREFIX}{provider}:{kind}:{day.isoformat()}"


async def reserve(redis, settings: Settings, provider: str) -> None:
    """占一个名额；超额抛 QuotaExceeded，Redis 出错抛 QuotaUnavailable。未设上限的上游也计数，只是不拦。"""
    limit = settings.daily_limits().get(provider)
    day = beijing_today()
    calls_key, rejected_key = _key(provider, "calls", day), _key(provider, "rejected", day)
    rejected = 0
    try:
        with anyio.fail_after(REDIS_TIMEOUT_SECONDS):
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(calls_key)
                pipe.expire(calls_key, COUNTER_TTL_SECONDS)
                used, _ = await pipe.execute()
            if limit is not None and used > limit:
                # 先加后判，超了再退回：INCR 是原子的，并发请求不会一起越过上限。
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.decr(calls_key)
                    pipe.incr(rejected_key)
                    pipe.expire(rejected_key, COUNTER_TTL_SECONDS)
                    _, rejected, _ = await pipe.execute()
    except Exception:
        # 不因 Redis 故障放行付费调用，也不向调用方泄露连接信息。
        logger.warning("provider_quota_unavailable", provider=provider)
        raise QuotaUnavailable from None
    if limit is None:
        return
    if used > limit:
        logger.warning("provider_daily_quota_exceeded", provider=provider, limit=limit)
        if rejected == 1:
            _alert(
                settings,
                f"【image-service】{provider} 今日上游调用已达每日上限 {limit:,} 次，之后跳过该上游"
                "（生图按备用链换模型，参考图搜索跳过），北京时间 0 点重置。上限在 image .env 的 PROVIDER_DAILY_LIMITS。",
            )
        raise QuotaExceeded(provider, limit)
    if limit and used == max(1, math.ceil(limit * settings.quota_alert_ratio)):
        _alert(
            settings,
            f"【image-service】{provider} 今日上游已调用 {used:,} 次，达到每日上限 {limit:,} 的 "
            f"{settings.quota_alert_ratio:.0%}。用满后当天跳过该上游，北京时间 0 点重置。",
        )


async def daily_summary(redis, settings: Settings, day: date) -> dict:
    """某天各上游的调用 / 被拦次数，供网关日报读取；只含计数，不含产品或用户。"""
    limits = settings.daily_limits()
    providers = list(QUOTA_PROVIDERS)
    keys = [_key(p, kind, day) for p in providers for kind in ("calls", "rejected")]
    try:
        with anyio.fail_after(REDIS_TIMEOUT_SECONDS):
            values = await redis.mget(keys)
    except Exception:
        return {"day": day.isoformat(), "available": False, "providers": []}
    counts = [int(v or 0) for v in values]
    return {
        "day": day.isoformat(),
        "available": True,
        "providers": [
            {"provider": p, "limit": limits.get(p), "used": counts[2 * i], "rejected": counts[2 * i + 1]}
            for i, p in enumerate(providers)
        ],
    }


def _alert(settings: Settings, text: str) -> None:
    webhook = settings.quota_alert_webhook.get_secret_value().strip()
    if not webhook:
        return
    # 告警不阻塞生图；任务引用留在集合里，防止被垃圾回收。
    task = asyncio.create_task(_send_feishu(webhook, settings.quota_alert_sign_secret.get_secret_value().strip(), text))
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def _feishu_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def _send_feishu(webhook: str, secret: str, text: str) -> None:
    """日志里只记状态码和异常类型：webhook 地址本身就是密钥。"""
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    if secret:
        timestamp = str(int(time.time()))
        signature = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(signature).decode()
    try:
        async with _feishu_client() as client:
            resp = await client.post(webhook, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("quota_alert_failed", error=type(exc).__name__)
        return
    code = None
    if resp.status_code == 200:
        try:
            code = resp.json().get("code")
        except ValueError:
            code = "invalid_json"
    if resp.status_code != 200 or code != 0:
        logger.warning("quota_alert_failed", status=resp.status_code, feishu_code=code)
