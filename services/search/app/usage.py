from datetime import UTC, date, datetime, timedelta

from app.cache import get_redis
from app.logger import get_logger
from app.models import ProviderRecordedUsage, ProviderRecordedUsageDay

log = get_logger()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _usage_day(now: datetime | None = None) -> str:
    value = now or _utc_now()
    return value.astimezone(UTC).date().isoformat()


async def record_search_credits(provider: str, credits_used: int | None) -> None:
    if credits_used is None or credits_used <= 0:
        return

    day = _usage_day()
    credits_key = f"usage:{provider}:credits:day:{day}"
    requests_key = f"usage:{provider}:requests:day:{day}"

    try:
        redis = await get_redis()
        pipe = redis.pipeline()
        pipe.incrby(credits_key, credits_used)
        pipe.incr(requests_key)
        await pipe.execute()
    except Exception as exc:
        log.warning("usage_record_failed", provider=provider, error=str(exc))


async def get_recorded_provider_usage(
    provider: str,
    *,
    period_start: str | None,
    period_end: str | None,
) -> ProviderRecordedUsage:
    days = _days_for_period(period_start, period_end)
    credit_keys = [f"usage:{provider}:credits:day:{day.isoformat()}" for day in days]
    request_keys = [f"usage:{provider}:requests:day:{day.isoformat()}" for day in days]

    credits_used = 0
    request_count = 0
    daily: list[ProviderRecordedUsageDay] = []
    available = True
    try:
        redis = await get_redis()
        credit_values = await redis.mget(credit_keys) if credit_keys else []
        request_values = await redis.mget(request_keys) if request_keys else []
        daily = [
            ProviderRecordedUsageDay(
                date=day.isoformat(),
                credits_used=_safe_int(credit_value),
                request_count=_safe_int(request_value),
            )
            for day, credit_value, request_value in zip(days, credit_values, request_values, strict=True)
        ]
        credits_used = sum(day.credits_used for day in daily)
        request_count = sum(day.request_count for day in daily)
    except Exception as exc:
        available = False
        log.warning("usage_summary_failed", provider=provider, error=str(exc))

    return ProviderRecordedUsage(
        provider=provider,
        available=available,
        credits_used=credits_used,
        request_count=request_count,
        period_start=period_start,
        period_end=period_end,
        daily=daily,
    )


def _days_for_period(period_start: str | None, period_end: str | None) -> list[date]:
    start = _parse_iso_datetime(period_start)
    end = _parse_iso_datetime(period_end) or _utc_now()
    if start is None:
        now = _utc_now()
        start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if end < start:
        end = start

    start_date = start.astimezone(UTC).date()
    end_date = min(end.astimezone(UTC).date(), _utc_now().date())
    days: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_int(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
