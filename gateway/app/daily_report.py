"""每日用量日报：北京时间每天 daily_report_hour 点后，把前一天的调用汇总推到飞书「AI 成本&额度」群。

- 各服务 / 产品的调用量来自网关自己的用量记录（只读）。MCP 的握手消息（initialize、tools/list、
  SSE 等）不算调用，只统计 tools/call 和 REST 请求。
- 服务内部实际调了哪家上游、被按上游的每日额度拦下几次，网关看不到，向服务拉取：
  search 的 /usage/daily（另含转备用次数、Firecrawl 剩余 credits）、image 的 /v1/quota、reader 的 /usage/daily。

手动跑：python -m app.daily_report [--day YYYY-MM-DD] [--print] [--test]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.feishu import send_feishu_text
from app.proxy import counts_as_call

logger = logging.getLogger("gateway")

# 中国没有夏令时，固定 +8，不依赖容器里的 tzdata。
BEIJING = timezone(timedelta(hours=8))
_CHECK_INTERVAL_SECONDS = 300
# 发送失败（网络、webhook 配错）时每天最多尝试几次，避免配置错误时每 5 分钟刷一次。
_MAX_ATTEMPTS_PER_DAY = 3
_STATE_KEEP_DAYS = 10
_ROLE_LABELS = {"primary": "（主）", "fallback": "（备用）"}
# 各服务按上游计数的接口；image / reader 的字段统一为 day / available / providers[provider, limit, used, rejected]，
# maps 没有每日上限（额度在高德、腾讯那边），字段是 providers[provider, requests, failed, quota, skipped] 和 fallbacks。
UPSTREAM_SUMMARY_PATHS = {
    "search": "/usage/daily",
    "image": "/v1/quota",
    "reader": "/usage/daily",
    "maps": "/usage/daily",
}
# 有请求却没有任何上游计数时的常见原因（手动查 3 天前的日期时计数已过期）
_NO_UPSTREAM_REASONS = {"image": "全部命中缓存", "reader": "全部直接抓取"}


@dataclass
class ServiceStats:
    calls: int = 0
    failed: int = 0
    # 被网关每日上限拦下的请求，不计入 calls / failed
    limited: int = 0
    tools: Counter = field(default_factory=Counter)


@dataclass
class DayStats:
    day: date
    calls: int = 0
    failed: int = 0
    limited: int = 0
    services: dict[str, ServiceStats] = field(default_factory=lambda: defaultdict(ServiceStats))
    products: Counter = field(default_factory=Counter)


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
                   SUM(outcome != 'limited') AS requests,
                   SUM(outcome != 'limited' AND (outcome != 'ok' OR status >= 400)) AS failed,
                   SUM(outcome = 'limited') AS limited
            FROM requests WHERE ts >= ? AND ts < ?
            GROUP BY service, app_id, path, tool
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    for service, app_id, path, tool, requests, failed, limited in rows:
        if not counts_as_call(service, path, tool):
            continue
        svc = stats.services[service]
        svc.calls += requests
        svc.failed += failed
        svc.limited += limited
        if tool and requests:
            svc.tools[tool] += requests
        stats.calls += requests
        stats.failed += failed
        stats.limited += limited
        if requests:
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


def _quota_lines(service: str, summary: dict | None, gateway_calls: int) -> list[str]:
    """image / reader 的上游用量，一行：上游 调用 / 上限，被拦时附次数；没用量也没调用时不占篇幅。"""
    if summary is None:
        return [f"{service} 上游：获取失败"]
    if not summary.get("available", False):
        return [f"{service} 上游：计数不可用"]
    active = [p for p in summary.get("providers", []) if p.get("used") or p.get("rejected")]
    if active:
        parts = []
        for p in active:
            limit = p.get("limit")
            part = f"{p['provider']} {p.get('used', 0):,}" + (f" / {limit:,}" if limit is not None else "（不限额）")
            if p.get("rejected"):
                part += f"（拦下 {p['rejected']:,} 次）"
            parts.append(part)
        return [f"{service} 上游（调用 / 每日上限）：" + " · ".join(parts)]
    if gateway_calls:
        return [f"{service} 上游：没有调用上游（{_NO_UPSTREAM_REASONS[service]}，或计数已过 3 天保留期）"]
    return []


def _maps_lines(summary: dict | None, gateway_calls: int) -> list[str]:
    """maps 的上游用量，一行：每家 请求 / 失败，额度用尽、记着用尽没发的另附；再加转腾讯几次。没用量也没调用时不出。"""
    if summary is None:
        return ["maps 上游：获取失败"]
    if not summary.get("available", False):
        return ["maps 上游：计数不可用"]
    active = [p for p in summary.get("providers", []) if p.get("requests") or p.get("skipped")]
    if active:
        parts = []
        for p in active:
            part = f"{p['provider']} {p.get('requests', 0):,} / {p.get('failed', 0):,}"
            extra = []
            if p.get("quota"):
                extra.append(f"额度用尽 {p['quota']:,} 次")
            if p.get("skipped"):
                extra.append(f"因用尽跳过 {p['skipped']:,} 次")
            parts.append(part + (f"（{'，'.join(extra)}）" if extra else ""))
        return [f"maps 上游（请求 / 失败）：{' · '.join(parts)} · 转腾讯 {summary.get('fallbacks', 0):,} 次"]
    if gateway_calls:
        return ["maps 上游：没有请求上游（参数不对、没有当前位置，或计数已过 3 天保留期）"]
    return []


def _limit_note(svc: ServiceStats, limit: int | None) -> str:
    # 只在用量过半或有拦截时提示上限，平时不占篇幅
    if limit is None or not (svc.limited or svc.calls >= limit / 2):
        return ""
    note = f"，上限 {limit:,}"
    return note + (f"，拦下 {svc.limited:,} 次" if svc.limited else "")


def render_report(
    stats: DayStats,
    previous: DayStats | None,
    search_summary: dict | None,
    limits: Mapping[str, int] | None = None,
    upstream_summaries: Mapping[str, dict | None] | None = None,
) -> str:
    """upstream_summaries：image / reader / maps 的上游计数（键缺失表示没拉取，不出这一段）。"""
    limits = limits or {}
    upstream_summaries = upstream_summaries or {}
    lines = [f"【agent-tools 日报】{stats.day.strftime('%m-%d')}（北京时间）"]
    overview = f"调用 {stats.calls:,} 次，失败 {stats.failed:,} 次"
    if stats.limited:
        overview += f"，被每日上限拦下 {stats.limited:,} 次"
    if previous is not None:
        overview += f"（前一天 {previous.calls:,} 次）"
    lines.append(overview)

    if stats.services:
        lines.append("按服务（调用 / 失败）：")
        for name, svc in sorted(stats.services.items(), key=lambda item: (-item[1].calls, item[0])):
            lines.append(
                f"· {name} {svc.calls:,} / {svc.failed:,}{_fmt_tools(svc.tools)}{_limit_note(svc, limits.get(name))}"
            )
        lines.append("按产品：" + " · ".join(f"{app} {count:,}" for app, count in _ranked(stats.products)))

    def calls(service: str) -> int:
        return stats.services[service].calls if service in stats.services else 0

    lines.extend(_search_lines(search_summary, calls("search")))
    for service in ("image", "reader"):
        if service in upstream_summaries:
            lines.extend(_quota_lines(service, upstream_summaries[service], calls(service)))
    if "maps" in upstream_summaries:
        lines.extend(_maps_lines(upstream_summaries["maps"], calls("maps")))
    return "\n".join(lines)


async def fetch_upstream_summary(client: httpx.AsyncClient | None, service: str, day: date) -> dict | None:
    if client is None:
        return None
    try:
        resp = await client.get(UPSTREAM_SUMMARY_PATHS[service], params={"day": day.isoformat()})
    except httpx.HTTPError as exc:
        logger.warning("daily_report_upstream_summary_failed: %s %s", service, type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("daily_report_upstream_summary_failed: %s status %s", service, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("daily_report_upstream_summary_failed: %s invalid json", service)
        return None
    if not isinstance(data, dict):
        logger.warning("daily_report_upstream_summary_failed: %s invalid json", service)
        return None
    return data


async def build_report(settings: Settings, clients: Mapping[str, httpx.AsyncClient], day: date) -> str:
    """clients：服务名 → 指向该服务的 client（网关转发用的同一批）。"""
    stats, previous = await asyncio.gather(
        asyncio.to_thread(collect_day_stats, settings.usage_db_path, day),
        asyncio.to_thread(collect_day_stats, settings.usage_db_path, day - timedelta(days=1)),
    )
    services = tuple(UPSTREAM_SUMMARY_PATHS)
    summaries = await asyncio.gather(*(fetch_upstream_summary(clients.get(s), s, day) for s in services))
    by_service = dict(zip(services, summaries, strict=True))
    search_summary = by_service.pop("search")
    return render_report(stats, previous, search_summary, settings.daily_limits(), by_service)


async def send_daily_report(
    settings: Settings, clients: Mapping[str, httpx.AsyncClient], day: date, *, test: bool = False
) -> bool:
    text = await build_report(settings, clients, day)
    if test:
        text = "【测试】" + text
    ok = await send_feishu_text(
        settings.daily_report_webhook.get_secret_value().strip(),
        settings.daily_report_sign_secret.get_secret_value().strip(),
        text,
        purpose="daily_report",
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


async def run_due_report(settings: Settings, clients: Mapping[str, httpx.AsyncClient], now: datetime) -> str | None:
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
        ok = await send_daily_report(settings, clients, day)
    except Exception as exc:
        logger.warning("daily_report_failed: %s", type(exc).__name__)
        ok = False
    if ok:
        entry["status"] = "sent"
        _save_state(path, state, day)
    return "sent" if ok else "failed"


async def run_daily_report_loop(settings: Settings, clients: Mapping[str, httpx.AsyncClient]) -> None:
    logger.info("daily_report_scheduler_started: hour %s", settings.daily_report_hour)
    while True:
        try:
            await run_due_report(settings, clients, datetime.now(UTC))
        except Exception as exc:
            # 状态文件写不了等情况：记一笔，下个周期再试，不能让循环退出。
            logger.warning("daily_report_tick_failed: %s", type(exc).__name__)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _cli(day: date, *, print_only: bool, test: bool) -> None:
    settings = Settings()
    upstreams = settings.upstreams()
    clients = {
        name: httpx.AsyncClient(base_url=upstreams[name].base_url, timeout=30, trust_env=False)
        for name in UPSTREAM_SUMMARY_PATHS
    }
    try:
        if print_only:
            print(await build_report(settings, clients, day))
        else:
            print("sent" if await send_daily_report(settings, clients, day, test=test) else "failed")
    finally:
        for client in clients.values():
            await client.aclose()


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
