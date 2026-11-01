"""按上游的每日调用额度（北京时间自然日），兜底防止批量实验或失控循环把付费上游打穿。

- 上游：firecrawl、tavily、jina。一次读取真正请求某家上游时算一次；同一读取内换 key、Jina 重试不另算。
- 用满后当天跳过该上游：Firecrawl / Tavily 不请求，Jina 返回 daily_quota_exceeded。
- 服务是单进程，计数在内存里，每次变化原子写入状态文件（保留 3 天），重启后接着算；
  状态文件读不了时从 0 计、不拦读取，只记日志。
- 用到 QUOTA_ALERT_RATIO 和用满时各推一次飞书巡检群；计数随状态文件持久化，重启后不会重复告警。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("reader_service")

PROVIDERS = ("firecrawl", "tavily", "jina")
BEIJING = ZoneInfo("Asia/Shanghai")
# 计数保留 3 天，够日报读前一天
RETENTION_DAYS = 3
DEFAULT_LIMITS = "firecrawl=300,tavily=300,jina=300"
_alert_tasks: set[asyncio.Task] = set()


def parse_limits(raw: str) -> dict[str, int]:
    """'firecrawl=300,tavily=0' → {provider: 次数}；写错格式或未知上游直接报错，服务起不来。"""
    limits: dict[str, int] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        name = name.strip().lower()
        if not sep or not name:
            raise ValueError(f"PROVIDER_DAILY_LIMITS 格式应为 上游=次数：{item!r}")
        if name not in PROVIDERS:
            raise ValueError(
                f"PROVIDER_DAILY_LIMITS 有未知上游 {name!r}，可选 {', '.join(PROVIDERS)}"
            )
        try:
            count = int(value.strip())
        except ValueError:
            raise ValueError(
                f"PROVIDER_DAILY_LIMITS 的次数应为整数：{item!r}"
            ) from None
        if count < 0:
            raise ValueError(f"PROVIDER_DAILY_LIMITS 的次数不能为负：{item!r}")
        limits[name] = count
    return limits


def beijing_today(now: datetime | None = None) -> date:
    return (now or datetime.now(BEIJING)).astimezone(BEIJING).date()


class DailyQuota:
    def __init__(
        self,
        limits: dict[str, int],
        state_path: str = "",
        alert_ratio: float = 0.8,
        alert: Callable[[str], None] | None = None,
    ):
        self.limits = limits
        self._path = Path(state_path) if state_path else None
        self._ratio = alert_ratio
        self._alert = alert
        # 日期 -> 上游 -> {"calls": n, "rejected": m}
        self._days: dict[str, dict[str, dict[str, int]]] = self._load()

    def try_use(self, provider: str, now: datetime | None = None) -> bool:
        """占一个名额；当天已用满返回 False。未设上限的上游也计数，只是不拦。"""
        today = beijing_today(now)
        counts = self._counts(today, provider)
        limit = self.limits.get(provider)
        if limit is not None and counts["calls"] >= limit:
            counts["rejected"] += 1
            self._save(today)
            logger.warning(
                "reader_daily_quota_exceeded provider=%s limit=%s", provider, limit
            )
            if counts["rejected"] == 1:
                self._notify(
                    f"【reader-service】{provider} 今日上游调用已达每日上限 {limit:,} 次，之后跳过该上游"
                    "（Firecrawl / Tavily 不再请求，Jina 返回 daily_quota_exceeded），北京时间 0 点重置。"
                    "上限在 reader .env 的 PROVIDER_DAILY_LIMITS。"
                )
            return False
        counts["calls"] += 1
        self._save(today)
        if limit and counts["calls"] == max(1, math.ceil(limit * self._ratio)):
            self._notify(
                f"【reader-service】{provider} 今日上游已调用 {counts['calls']:,} 次，达到每日上限 {limit:,} 的 "
                f"{self._ratio:.0%}。用满后当天跳过该上游，北京时间 0 点重置。"
            )
        return True

    def summary(self, day: date) -> dict:
        """某天各上游的调用 / 被拦次数，供网关日报读取；只含计数。超过保留期的日期全是 0。"""
        stored = self._days.get(day.isoformat(), {})
        return {
            "day": day.isoformat(),
            "available": True,
            "providers": [
                {
                    "provider": name,
                    "limit": self.limits.get(name),
                    "used": stored.get(name, {}).get("calls", 0),
                    "rejected": stored.get(name, {}).get("rejected", 0),
                }
                for name in PROVIDERS
            ],
        }

    def _counts(self, day: date, provider: str) -> dict[str, int]:
        return self._days.setdefault(day.isoformat(), {}).setdefault(
            provider, {"calls": 0, "rejected": 0}
        )

    def _notify(self, text: str) -> None:
        if self._alert is not None:
            self._alert(text)

    def _load(self) -> dict[str, dict[str, dict[str, int]]]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
            days = {
                str(day): {
                    str(name): {
                        "calls": int(c.get("calls", 0)),
                        "rejected": int(c.get("rejected", 0)),
                    }
                    for name, c in providers.items()
                }
                for day, providers in raw["days"].items()
            }
        except Exception as exc:
            # 读不了就从 0 计：额度是兜底，不因状态文件损坏挡住读取。
            logger.warning(
                "reader_quota_state_load_failed error=%s", type(exc).__name__
            )
            return {}
        return days

    def _save(self, today: date) -> None:
        oldest = (today - timedelta(days=RETENTION_DAYS - 1)).isoformat()
        for day in [d for d in self._days if d < oldest]:
            del self._days[day]
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, prefix=".quota_state.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(
                        {"days": self._days}, f, ensure_ascii=False, sort_keys=True
                    )
                os.replace(tmp, self._path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # 写不了不影响本次计数（内存里仍然有），只是重启后会丢。
            logger.warning(
                "reader_quota_state_save_failed error=%s", type(exc).__name__
            )


def feishu_alert(webhook: str, secret: str) -> Callable[[str], None] | None:
    """返回后台推送飞书的告警函数；没配 webhook 时返回 None（只写日志）。"""
    webhook, secret = webhook.strip(), secret.strip()
    if not webhook:
        return None

    def alert(text: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # 告警不阻塞读取；任务引用留在集合里，防止被垃圾回收。
        task = loop.create_task(send_feishu(webhook, secret, text))
        _alert_tasks.add(task)
        task.add_done_callback(_alert_tasks.discard)

    return alert


def _feishu_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def send_feishu(webhook: str, secret: str, text: str) -> None:
    """日志里只记状态码和异常类型：webhook 地址本身就是密钥。"""
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    if secret:
        timestamp = str(int(time.time()))
        signature = hmac.new(
            f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256
        ).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(signature).decode()
    try:
        async with _feishu_client() as client:
            resp = await client.post(webhook, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("reader_quota_alert_failed error=%s", type(exc).__name__)
        return
    code = None
    if resp.status_code == 200:
        try:
            code = resp.json().get("code")
        except ValueError:
            code = "invalid_json"
    if resp.status_code != 200 or code != 0:
        logger.warning(
            "reader_quota_alert_failed status=%s feishu_code=%s", resp.status_code, code
        )


def from_env() -> DailyQuota:
    ratio = float(os.getenv("QUOTA_ALERT_RATIO", "0.8"))
    if not 0 < ratio <= 1:
        raise ValueError("QUOTA_ALERT_RATIO 应在 (0, 1] 之间")
    return DailyQuota(
        parse_limits(os.getenv("PROVIDER_DAILY_LIMITS", DEFAULT_LIMITS)),
        os.getenv("QUOTA_STATE_PATH", ""),
        ratio,
        feishu_alert(
            os.getenv("QUOTA_ALERT_WEBHOOK", ""),
            os.getenv("QUOTA_ALERT_SIGN_SECRET", ""),
        ),
    )
