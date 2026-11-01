"""每日用量日报：北京时间每天 daily_report_hour 点后，把前一天的调用汇总推到飞书「AI 成本&额度」群。

- 各服务 / 产品的调用量来自网关自己的用量记录（只读）。MCP 的握手消息（initialize、tools/list、
  SSE 等）不算调用，只统计 tools/call 和 REST 请求。
- search 内部实际调了哪家上游、转了几次备用，网关看不到，向 search 的 /usage/daily 拉取。

手动跑：python -m app.daily_report [--day YYYY-MM-DD] [--print] [--test]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger("gateway")

# 中国没有夏令时，固定 +8，不依赖容器里的 tzdata。
BEIJING = timezone(timedelta(hours=8))
_CHECK_INTERVAL_SECONDS = 300
# 发送失败（网络、webhook 配错）时每天最多尝试几次，避免配置错误时每 5 分钟刷一次。
_MAX_ATTEMPTS_PER_DAY = 3
_STATE_KEEP_DAYS = 10
_ROLE_LABELS = {"primary": "（主）", "fallback": "（备用）"}


@dataclass
class ServiceStats:
    calls: int = 0
    failed: int = 0
    tools: Counter = field(default_factory=Counter)


@dataclass
class DayStats:
    day: date
    calls: int = 0
    failed: int = 0
    services: dict[str, ServiceStats] = field(default_factory=lambda: defaultdict(ServiceStats))
    products: Counter = field(default_factory=Counter)


def _is_mcp_path(path: str) -> bool:
    # 与 proxy.mcp_tool_name 的判断一致；image-service 的 MCP 路径带尾斜杠。
    return path.rstrip("/").rsplit("/", 1)[-1] == "mcp"


def day_bounds_utc(day: date) -> tuple[str, str]:
    """北京时间某天在用量表里对应的 UTC 时间范围 [start, end)。ts 存的是 UTC isoformat，可以直接按字符串比较。"""
    start = datetime(day.year, day.month, day.day, tzinfo=BEIJING)
    return start.astimezone(UTC).isoformat(), (start + timedelta(days=1)).astimezone(UTC).isoformat()


def collect_day_stats(db_path: str, day: date) -> DayStats:
    start, end = day_bounds_utc(day)
    stats = DayStats(day=day)
    if not Path(db_path).exists():
        return stats
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT service, app_id, path, tool,
                   COUNT(*) AS requests,
                   SUM(outcome != 'ok' OR status >= 400) AS failed
            FROM requests WHERE ts >= ? AND ts < ?
            GROUP BY service, app_id, path, tool
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    for service, app_id, path, tool, requests, failed in rows:
        if _is_mcp_path(path) and not tool:
            continue
        svc = stats.services[service]
        svc.calls += requests
        svc.failed += failed
        if tool:
            svc.tools[tool] += requests
        stats.calls += requests
        stats.failed += failed
        stats.products[app_id] += requests
    return stats


def _ranked(counter: Counter) -> list[tuple[str, int]]:
    # 次数降序，同次数按名字排，保证每天的输出顺序稳定
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def _fmt_tools(tools: Counter) -> str:
    if not tools:
        return ""
    return "（" + "、".join(f"{name} {count}" for name, count in _ranked(tools)[:3]) + "）"


def _beijing_mmdd(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BEIJING).strftime("%m-%d")
    except ValueError:
        return None


def _search_lines(summary: dict | None, gateway_search_calls: int) -> list[str]:
    if summary is None:
        return ["search 上游：获取失败"]
    if not summary.get("available", False):
        return ["search 上游：计数不可用（search 读不到 Redis）"]
    providers = [p for p in summary.get("providers", []) if p.get("registered") or p.get("used") or p.get("rejected")]
    fallback = summary.get("fallback") or {}
    has_counts = any(p.get("used") or p.get("rejected") for p in providers) or any(fallback.values())
    lines: list[str] = []
    if has_counts:
        lines.append("search 上游（调用 / 每日上限）：")
        for p in providers:
            limit = p.get("limit")
            line = f"· {p['provider']}{_ROLE_LABELS.get(p.get('role'), '')} {p.get('used', 0)}"
            line += f" / {limit}" if limit is not None else "（不限额）"
            if p.get("rejected"):
                line += f"，被额度拦下 {p['rejected']} 次"
            lines.append(line)
        total = sum(fallback.values())
        detail = f"（报错 {fallback.get('error', 0)}，结果不足 {fallback.get('insufficient', 0)}）" if total else ""
        lines.append(f"· 转备用 {total} 次{detail}")
    elif gateway_search_calls:
        # 有请求却没有任何上游计数：通常是全部命中缓存（缓存命中不占额度）；手动查 3 天前的日期时计数已过期。
        lines.append("search 上游：没有调用上游（全部命中缓存，或计数已过 3 天保留期）")
    credits = summary.get("firecrawl_remaining_credits")
    if credits is not None:
        period_end = _beijing_mmdd(summary.get("firecrawl_billing_period_end"))
        lines.append(f"Firecrawl 剩余 {credits:,} credits" + (f"（计费周期至 {period_end}）" if period_end else ""))
    return lines


def render_report(stats: DayStats, previous: DayStats | None, search_summary: dict | None) -> str:
    lines = [f"【agent-tools 日报】{stats.day.strftime('%m-%d')}（北京时间）"]
    overview = f"调用 {stats.calls:,} 次，失败 {stats.failed:,} 次"
    if previous is not None:
        overview += f"（前一天 {previous.calls:,} 次）"
    lines.append(overview)

    if stats.services:
        lines.append("按服务（调用 / 失败）：")
        for name, svc in sorted(stats.services.items(), key=lambda item: (-item[1].calls, item[0])):
            lines.append(f"· {name} {svc.calls:,} / {svc.failed:,}{_fmt_tools(svc.tools)}")
        lines.append("按产品：" + " · ".join(f"{app} {count:,}" for app, count in _ranked(stats.products)))

    search_calls = stats.services["search"].calls if "search" in stats.services else 0
    lines.extend(_search_lines(search_summary, search_calls))
    return "\n".join(lines)


async def fetch_search_summary(client: httpx.AsyncClient, day: date) -> dict | None:
    try:
        resp = await client.get("/usage/daily", params={"day": day.isoformat()})
    except httpx.HTTPError as exc:
        logger.warning("daily_report_search_summary_failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("daily_report_search_summary_failed: status %s", resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("daily_report_search_summary_failed: invalid json")
        return None


async def build_report(settings: Settings, search_client: httpx.AsyncClient, day: date) -> str:
    stats, previous = await asyncio.gather(
        asyncio.to_thread(collect_day_stats, settings.usage_db_path, day),
        asyncio.to_thread(collect_day_stats, settings.usage_db_path, day - timedelta(days=1)),
    )
    summary = await fetch_search_summary(search_client, day)
    return render_report(stats, previous, summary)


def _feishu_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def send_feishu_text(webhook: str, secret: str, text: str) -> bool:
    """发飞书自定义机器人文本消息，返回 code 0 时为 True。日志里只记状态码和异常类型：webhook 地址即密钥。"""
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
        logger.warning("daily_report_send_failed: %s", type(exc).__name__)
        return False
    if resp.status_code != 200:
        logger.warning("daily_report_send_failed: status %s", resp.status_code)
        return False
    try:
        code = resp.json().get("code", 0)
    except ValueError:
        code = "invalid json"
    if code != 0:
        logger.warning("daily_report_send_failed: feishu code %s", code)
        return False
    return True


async def send_daily_report(
    settings: Settings, search_client: httpx.AsyncClient, day: date, *, test: bool = False
) -> bool:
    text = await build_report(settings, search_client, day)
    if test:
        text = "【测试】" + text
    ok = await send_feishu_text(
        settings.daily_report_webhook.get_secret_value().strip(),
        settings.daily_report_sign_secret.get_secret_value().strip(),
        text,
    )
    if ok:
        logger.info("daily_report_sent: %s%s", day.isoformat(), " (test)" if test else "")
    return ok


def due_report_day(now: datetime, hour: int) -> date | None:
    """到了发送时刻返回要汇报的日期（北京时间前一天），没到返回 None。"""
    local = now.astimezone(BEIJING)
    if local.hour < hour:
        return None
    return local.date() - timedelta(days=1)


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict, today: date) -> None:
    cutoff = (today - timedelta(days=_STATE_KEEP_DAYS)).isoformat()
    state = {day: entry for day, entry in state.items() if day >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换，避免写一半时进程退出留下坏文件。
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".daily_report_state.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


async def run_due_report(settings: Settings, search_client: httpx.AsyncClient, now: datetime) -> str | None:
    """定时任务的一次检查：到点且当天还没发成功才发，每天最多尝试 _MAX_ATTEMPTS_PER_DAY 次。"""
    day = due_report_day(now, settings.daily_report_hour)
    if day is None:
        return None
    path = Path(settings.daily_report_state_path)
    state = _load_state(path)
    key = day.isoformat()
    entry = state.get(key) if isinstance(state.get(key), dict) else {}
    if entry.get("status") == "sent" or entry.get("attempts", 0) >= _MAX_ATTEMPTS_PER_DAY:
        return None
    entry["attempts"] = entry.get("attempts", 0) + 1
    state[key] = entry
    _save_state(path, state, day)
    try:
        ok = await send_daily_report(settings, search_client, day)
    except Exception as exc:
        logger.warning("daily_report_failed: %s", type(exc).__name__)
        ok = False
    if ok:
        entry["status"] = "sent"
        _save_state(path, state, day)
    return "sent" if ok else "failed"


async def run_daily_report_loop(settings: Settings, search_client: httpx.AsyncClient) -> None:
    logger.info("daily_report_scheduler_started: hour %s", settings.daily_report_hour)
    while True:
        try:
            await run_due_report(settings, search_client, datetime.now(UTC))
        except Exception as exc:
            # 状态文件写不了等情况：记一笔，下个周期再试，不能让循环退出。
            logger.warning("daily_report_tick_failed: %s", type(exc).__name__)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _cli(day: date, *, print_only: bool, test: bool) -> None:
    settings = Settings()
    async with httpx.AsyncClient(base_url=settings.search_upstream, timeout=30, trust_env=False) as search_client:
        if print_only:
            print(await build_report(settings, search_client, day))
        else:
            print("sent" if await send_daily_report(settings, search_client, day, test=test) else "failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-tools 网关每日用量日报")
    parser.add_argument("--day", help="北京时间日期 YYYY-MM-DD，默认昨天")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印，不发送")
    parser.add_argument("--test", action="store_true", help="标题加【测试】；手动发送不影响每天的定时发送")
    args = parser.parse_args()
    day = date.fromisoformat(args.day) if args.day else datetime.now(BEIJING).date() - timedelta(days=1)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli(day, print_only=args.print_only, test=args.test))


if __name__ == "__main__":
    main()
