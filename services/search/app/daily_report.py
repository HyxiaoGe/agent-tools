"""每日搜索用量日报。

北京时间每天 DAILY_REPORT_HOUR 点后，把前一天各 provider 的上游调用量、转备用次数、
Firecrawl 剩余 credits 推到飞书「AI 成本&额度」群。数据来自 budget 的 Redis 计数（保留 3 天）。

手动跑：python -m app.daily_report [--day YYYY-MM-DD] [--print] [--test]
"""

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta

from app.budget import BEIJING, budget_day, get_budget_status, get_fallback_counts, send_feishu_text
from app.cache import close_redis, get_redis
from app.config import parse_provider_list, settings
from app.logger import get_logger
from app.providers.registry import get_firecrawl_usage, list_providers

log = get_logger()

_CHECK_INTERVAL_SECONDS = 300
_KEY_TTL_SECONDS = 3 * 86400
# 发送失败（网络、webhook 配错）时每天最多重试几次，避免配置错误时每 5 分钟刷一次日志。
_MAX_ATTEMPTS_PER_DAY = 3


def _sent_key(day: str) -> str:
    return f"report:daily:sent:{day}"


def _attempts_key(day: str) -> str:
    return f"report:daily:attempts:{day}"


def due_report_day(now: datetime) -> str | None:
    """到了发送时刻返回要汇报的日期（北京时间前一天），没到返回 None。"""
    local = now.astimezone(BEIJING)
    if local.hour < settings.DAILY_REPORT_HOUR:
        return None
    return (local.date() - timedelta(days=1)).isoformat()


def _provider_order(names: list[str]) -> list[str]:
    # 主 provider 在前，接着按备用链顺序，其余按字母序。
    preferred = [settings.SEARCH_PROVIDER, *parse_provider_list(settings.SEARCH_FALLBACK_PROVIDERS)]
    ordered = [name for name in dict.fromkeys(preferred) if name in names]
    return ordered + sorted(name for name in names if name not in ordered)


def _role(name: str) -> str:
    if name == settings.SEARCH_PROVIDER:
        return "（主）"
    if name in parse_provider_list(settings.SEARCH_FALLBACK_PROVIDERS):
        return "（备用）"
    return ""


def _beijing_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BEIJING).strftime("%m-%d")
    except ValueError:
        return None


async def build_report(day: str) -> str | None:
    """组装日报文本；当天没有任何计数（服务刚上线或 Redis 数据已过期）时返回 None。"""
    statuses = await get_budget_status(day)
    if statuses and not statuses[0].available:
        raise RuntimeError("Redis 不可用，读不到当日计数")
    fallback = await get_fallback_counts(day)

    registered = {item["name"] for item in list_providers()}
    by_name = {s.provider: s for s in statuses if s.provider in registered or s.used or s.rejected}
    if not any(s.used or s.rejected for s in by_name.values()) and not any(fallback.values()):
        return None

    lines = [f"【搜索日报】{day[5:]}（北京时间）", "上游调用 / 每日上限："]
    for name in _provider_order(list(by_name)):
        status = by_name[name]
        line = f"· {name}{_role(name)} {status.used} / {status.limit}"
        if status.rejected:
            line += f"，被额度拦下 {status.rejected} 次"
        lines.append(line)

    total_fallback = sum(fallback.values())
    if total_fallback:
        lines.append(
            f"转备用 {total_fallback} 次（主 provider 报错 {fallback['error']}，结果不足 {fallback['insufficient']}）"
        )
    else:
        lines.append("转备用 0 次")

    if "firecrawl" in registered:
        lines.append(await _firecrawl_line())
    return "\n".join(lines)


async def _firecrawl_line() -> str:
    try:
        usage = await get_firecrawl_usage()
    except Exception as exc:
        log.warning("daily_report_firecrawl_usage_failed", error=str(exc) or type(exc).__name__)
        return "Firecrawl 剩余 credits：查询失败"
    if not usage.available or usage.remaining_credits is None:
        return "Firecrawl 剩余 credits：未知"
    period_end = _beijing_date(usage.billing_period_end)
    suffix = f"（计费周期至 {period_end}）" if period_end else ""
    return f"Firecrawl 剩余 {usage.remaining_credits:,} credits{suffix}"


async def send_daily_report(day: str, *, test: bool = False) -> str:
    """返回 sent / no_data / failed。"""
    text = await build_report(day)
    if text is None:
        log.info("daily_report_no_data", day=day)
        return "no_data"
    if test:
        text = "【测试】" + text
    ok = await send_feishu_text(text, webhook=settings.DAILY_REPORT_WEBHOOK, secret=settings.DAILY_REPORT_SIGN_SECRET)
    log.info("daily_report_sent" if ok else "daily_report_send_failed", day=day, test=test)
    return "sent" if ok else "failed"


async def run_due_report(now: datetime) -> str | None:
    """定时任务的一次检查：到点且当天还没发过才发；同一天成功（或确认无数据）后不再发。"""
    day = due_report_day(now)
    if day is None:
        return None
    redis = await get_redis()
    if await redis.get(_sent_key(day)):
        return None
    attempts = await redis.incr(_attempts_key(day))
    if attempts == 1:
        await redis.expire(_attempts_key(day), _KEY_TTL_SECONDS)
    if attempts > _MAX_ATTEMPTS_PER_DAY:
        return None
    try:
        result = await send_daily_report(day)
    except Exception as exc:
        log.warning("daily_report_failed", day=day, attempt=attempts, error=str(exc) or type(exc).__name__)
        return "failed"
    if result != "failed":
        await redis.set(_sent_key(day), result, ex=_KEY_TTL_SECONDS)
    return result


async def run_daily_report_loop() -> None:
    log.info("daily_report_scheduler_started", hour=settings.DAILY_REPORT_HOUR)
    while True:
        try:
            await run_due_report(datetime.now(UTC))
        except Exception as exc:
            # Redis 暂时连不上等情况：记一笔，下个周期再试，不能让循环退出。
            log.warning("daily_report_tick_failed", error=str(exc) or type(exc).__name__)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _cli(day: str, *, print_only: bool, test: bool) -> None:
    try:
        if print_only:
            print(await build_report(day) or f"{day} 没有任何计数")
        else:
            print(await send_daily_report(day, test=test))
    finally:
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(description="搜索用量日报")
    parser.add_argument("--day", help="北京时间日期 YYYY-MM-DD，默认昨天")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印，不发送")
    parser.add_argument("--test", action="store_true", help="标题加【测试】；手动发送不影响每天的定时发送")
    args = parser.parse_args()
    day = date.fromisoformat(args.day).isoformat() if args.day else budget_day(datetime.now(UTC) - timedelta(days=1))
    asyncio.run(_cli(day, print_only=args.print_only, test=args.test))


if __name__ == "__main__":
    main()
