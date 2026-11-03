"""查天气的整套逻辑，MCP 工具 get_weather 用它：挑城市、算当地今天、整理逐日和逐小时、逐小时摘要、缓存。

2026-11-04 起从产品挪到这里，Fusion 和灵稿共用。以 Fusion 的 app/services/weather/qweather_forecast_tool.py 为底
（踩过坑的版本），带上灵稿 app/tools/weather.py 后来的改进。和两边原来不一样的地方：
- 当地今天按和风城市查询给的时区算。Fusion 是北京时间往前放一天，半夜跨天又赶上缓存会多留一个昨天；
  灵稿看逐小时的时区，取不到逐小时就退回北京时间，国外城市会丢掉当地今天。
- 城市查询也缓存（一天）；预报缓存两个产品共用。
- 问的日期不在预报里时，limitations 里明确标出 requested_date_not_covered，不用模型自己比日期。

和风的城市查询是模糊匹配，随便写什么都会返回几个城市，所以挑城市（select_city）只认名字出现在地名里的候选；
名字相同的用地名里写到的省、市、国家区分；还分不出就不按排名猜，把候选交给模型，它照候选的写法再查或者问用户。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.qweather import City, Day, Hour, QWeatherClient, QWeatherError

FORECAST_DAYS = 4
HOURLY_POINTS = 24
MAX_CANDIDATES = 5
MAX_LOCATION_LENGTH = 120
# 国外地点 rank 大于这个不参与匹配：乱写的地名、同名小地方实测都在 83–85
FOREIGN_MAX_RANK = 80
FORECAST_CACHE_SECONDS = 30 * 60
CITY_CACHE_SECONDS = 24 * 60 * 60
CACHE_MAX_ENTRIES = 2048
CALL_TIMEOUT_SECONDS = 20.0
SEP = "·"
DOMESTIC = "中国"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# 只用来从上级行政区的名字里取出常写的简称（浙江省 → 浙江），不判断意思
_ADMIN_SUFFIXES = ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市")
_COORDINATES = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*,\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*$")
_ADMIN_CODE = re.compile(r"^\d{6}$")
_UTC_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class ForecastError(Exception):
    """没查成。code 见 README；candidates 只在 ambiguous_location 时有，是候选地名的写法；detail 说参数哪里不对。"""

    def __init__(self, code: str, *, candidates: list[str] | None = None, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.candidates = candidates or []
        self.detail = detail


def parse_arguments(location: Any, requested_date: Any) -> tuple[str, date | None]:
    """地名只收地名：不收坐标和行政区代码（坐标该由产品从可信来源给，不让模型填）；日期只收 YYYY-MM-DD。"""

    text = location.strip() if isinstance(location, str) else ""
    if not text or len(text) > MAX_LOCATION_LENGTH:
        raise ForecastError(
            "invalid_arguments", detail=f"location must be a place name of 1-{MAX_LOCATION_LENGTH} characters"
        )
    if _COORDINATES.fullmatch(text) or _ADMIN_CODE.fullmatch(text):
        raise ForecastError(
            "invalid_arguments", detail="location must be a place name, not coordinates or an administrative code"
        )
    if requested_date is None or requested_date == "":
        return text, None
    try:
        on = date.fromisoformat(requested_date) if isinstance(requested_date, str) else None
    except ValueError:
        on = None
    if on is None or on.isoformat() != requested_date:  # fromisoformat 也收 20261010 这种写法，只认 YYYY-MM-DD
        raise ForecastError("invalid_arguments", detail="requested_date must be one calendar date in YYYY-MM-DD form")
    return text, on


def city_label(city: City) -> str:
    """候选的写法：国家（国内不写）·省·市·区县，相邻重复的只留一个。"""

    parts = ([city.country] if city.country and city.country != DOMESTIC else []) + [city.adm1, city.adm2, city.name]
    out: list[str] = []
    for part in parts:
        if part and (not out or out[-1] != part):
            out.append(part)
    return SEP.join(out)[:MAX_LOCATION_LENGTH]


def _short(name: str) -> str:
    for suffix in _ADMIN_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _hinted(city: City, query: str) -> bool:
    """地名里写到了这个候选的省、市或国家。"""

    for part in (city.adm1, city.adm2, city.country):
        short = _short(part)
        if len(short) >= 2 and short != city.name and short in query:
            return True
    return False


def _minor_foreign(city: City) -> bool:
    return bool(city.country) and city.country != DOMESTIC and city.rank is not None and city.rank > FOREIGN_MAX_RANK


def select_city(query: str, cities: list[City]) -> City:
    candidates = [city for city in cities if not _minor_foreign(city)]
    for city in candidates:  # 模型照候选的写法再查
        if city_label(city) == query:
            return city
    matched = [city for city in candidates if city.name in query]
    if not matched:
        raise ForecastError("location_not_found")
    matched = [city for city in matched if _hinted(city, query)] or matched
    # 地名一般从大写到小：取名字在地名里结束得最靠后的，再取名字最长的（更具体）
    end = max(query.rfind(city.name) + len(city.name) for city in matched)
    matched = [city for city in matched if query.rfind(city.name) + len(city.name) == end]
    longest = max(len(city.name) for city in matched)
    unique = list({city.location_id: city for city in matched if len(city.name) == longest}.values())
    labels = list(dict.fromkeys(city_label(city) for city in unique))
    if len(labels) == 1:  # 写法完全一样的重复条目分不出、也不用分，取第一个
        return unique[0]
    raise ForecastError("ambiguous_location", candidates=labels[:MAX_CANDIDATES])


def place_zone(city: City) -> tzinfo:
    """城市的时区：先用和风给的时区名，再用当前偏移，都没有就按北京时间。"""

    if city.tz:
        try:
            return ZoneInfo(city.tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if match := _UTC_OFFSET.fullmatch(city.utc_offset):
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        return timezone(-offset if sign == "-" else offset)
    return CHINA_TZ


def _number(text: str | None) -> int | float | None:
    try:
        value = float(text) if text not in (None, "") else None
    except ValueError:
        return None
    if value is None or not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else value


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def build_days(days: list[Day], today: date) -> list[dict[str, Any]]:
    """从当地今天起最多 4 天；日期或气温不对的、重复的、早于今天的（跨天时缓存里的昨天）都不要。"""

    out: list[dict[str, Any]] = []
    seen: set[date] = set()
    for day in days:
        try:
            on = date.fromisoformat(day.date)
        except ValueError:
            continue
        high, low = _number(day.temp_max), _number(day.temp_min)
        if on < today or on in seen or high is None or low is None:
            continue
        seen.add(on)
        out.append(
            _without_none(
                {
                    "date": on.isoformat(),
                    "weekday": WEEKDAYS[on.weekday()],
                    "day_weather": day.text_day,
                    "night_weather": day.text_night,
                    "high_c": high,
                    "low_c": low,
                    "day_wind_direction": day.wind_dir_day,
                    "day_wind_power": f"{day.wind_scale_day}级" if day.wind_scale_day else None,
                    "night_wind_direction": day.wind_dir_night,
                    "night_wind_power": f"{day.wind_scale_night}级" if day.wind_scale_night else None,
                }
            )
        )
        if len(out) == FORECAST_DAYS:
            break
    return out


def build_hourly(hours: list[Hour]) -> list[dict[str, Any]]:
    """最多 24 个小时；时间要带当地时区、要往后走，气温要是数字。"""

    out: list[dict[str, Any]] = []
    last: datetime | None = None
    for hour in hours[:HOURLY_POINTS]:
        try:
            at = datetime.fromisoformat(hour.time)
        except ValueError:
            continue
        temp = _number(hour.temp)
        if at.tzinfo is None or temp is None or (last is not None and at <= last):
            continue
        pop = _number(hour.pop)
        precip = _number(hour.precip)
        last = at
        out.append(
            _without_none(
                {
                    "time": at.isoformat(),
                    "temp_c": temp,
                    "weather": hour.text,
                    "pop": int(pop) if pop is not None and 0 <= pop <= 100 else None,
                    "precip_mm": precip if precip is not None and precip >= 0 else None,
                }
            )
        )
    return out


def summarize_hourly(hourly: list[dict[str, Any]]) -> dict[str, Any] | None:
    """逐小时明细给页面画图，模型拿这份摘要：时间窗、气温范围、降水概率最高的那个小时、有降水的小时。
    24 条明细交给模型，它容易漏看降水概率的高峰，也容易逐条复述。"""

    if not hourly:
        return None
    temps = [point["temp_c"] for point in hourly]
    summary: dict[str, Any] = {
        "window": f"{hourly[0]['time']} ~ {hourly[-1]['time']}",
        "temp_min_c": min(temps),
        "temp_max_c": max(temps),
    }
    with_pop = [point for point in hourly if point.get("pop") is not None]
    if with_pop:
        peak = max(with_pop, key=lambda point: point["pop"])  # 一样高的取最早的
        summary["max_pop_percent"] = peak["pop"]
        summary["max_pop_time"] = peak["time"]
    wet = [point for point in hourly if (point.get("precip_mm") or 0) > 0]
    summary["precipitation_hours"] = len(wet)
    if wet:
        summary["first_precipitation_time"] = wet[0]["time"]
        summary["last_precipitation_time"] = wet[-1]["time"]
        summary["total_precip_mm"] = round(sum(point["precip_mm"] for point in wet), 1)
    return summary


def wants_hourly(requested: date | None, today: date) -> bool:
    """逐小时只覆盖接下来 24 小时：没指定日期、问今天或明天时才取，省一次和风调用。"""

    return requested is None or 0 <= (requested - today).days <= 1


class _Cache:
    """进程内的短缓存，两个产品共用。只存非空的结果；条目太多时先清过期的，还多就全清。"""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, ...], tuple[float, datetime, Any]] = {}

    def get(self, key: tuple[str, ...], ttl: float) -> tuple[datetime, Any] | None:
        entry = self._entries.get(key)
        if entry is None or self._clock() - entry[0] >= ttl:
            return None
        return entry[1], entry[2]

    def put(self, key: tuple[str, ...], fetched_at: datetime, value: Any) -> None:
        now = self._clock()
        if len(self._entries) >= CACHE_MAX_ENTRIES:
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] < CITY_CACHE_SECONDS}
            if len(self._entries) >= CACHE_MAX_ENTRIES:
                self._entries.clear()
        self._entries[key] = (now, fetched_at, value)


class Forecaster:
    def __init__(
        self,
        client: Callable[[], QWeatherClient | None],
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._now = now
        self._cache = _Cache(clock)

    async def forecast(self, location: str, requested: date | None = None) -> dict[str, Any]:
        """整份结果（structuredContent）。没查成抛 ForecastError；上游的错误码原样带出来。"""

        client = self._client()
        if client is None:
            raise ForecastError("not_configured")
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                return await self._forecast(client, location, requested)
        except QWeatherError as error:
            raise ForecastError(error.code) from None
        except TimeoutError:
            raise ForecastError("call_timeout") from None

    async def _forecast(self, client: QWeatherClient, location: str, requested: date | None) -> dict[str, Any]:
        # 模型照候选的写法（“江西省·南昌·西湖”）再查时只拿最后一段去查，再按整个写法对；
        # 纯英文地名用英文查（lang=zh 时返回中文名，“Paris”对不上“巴黎”）
        lookup = location.rsplit(SEP, 1)[-1].strip() or location
        lang = "en" if lookup.isascii() else "zh"
        _, cities = await self._cached(
            ("city", lang, lookup), CITY_CACHE_SECONDS, lambda: client.cities(lookup, lang=lang)
        )
        city = select_city(location, cities)
        today = self._now().astimezone(place_zone(city)).date()

        def daily() -> Awaitable[tuple[datetime, list[Day]]]:
            return self._cached(
                ("daily", city.location_id), FORECAST_CACHE_SECONDS, lambda: client.daily(city.location_id)
            )

        hourly_wanted = wants_hourly(requested, today)
        if hourly_wanted:  # 和逐日同时取
            (fetched_at, days), hours = await asyncio.gather(daily(), self._hourly_or_empty(client, city.location_id))
        else:
            (fetched_at, days), hours = await daily(), []
        forecast_days = build_days(days, today)
        if not forecast_days:
            raise ForecastError("invalid_response")

        limitations: list[str] = []
        on = requested.isoformat() if requested else None
        requested_day = next((day for day in forecast_days if day["date"] == on), None)
        if requested and requested_day is None:
            limitations.append("requested_date_not_covered")
        if requested_day is not None:
            # 问的那天在预报里：只给那一天，逐小时也只留那天（当地日期）的小时，摘要随之只覆盖这些小时
            forecast_days = [requested_day]
        elif len(forecast_days) < FORECAST_DAYS:
            limitations.append("fewer_days")
        points = build_hourly(hours)
        if hourly_wanted and not points:
            limitations.append("hourly_unavailable")
        if requested_day is not None:
            points = [point for point in points if point["time"][:10] == on]

        result: dict[str, Any] = {"ok": True, "resolved_location": city_label(city), "local_today": today.isoformat()}
        if on:
            result["requested_date"] = on
        result["day_count"] = len(forecast_days)
        result["forecast_days"] = forecast_days
        if points:
            result["hourly"] = points
            result["hourly_summary"] = summarize_hourly(points)
        # 北京时间：10-07 Fusion 里模型把 UTC 的时刻当成本地时间念错过
        result["fetched_at"] = fetched_at.astimezone(CHINA_TZ).isoformat(timespec="seconds")
        result["limitations"] = limitations
        return result

    async def _hourly_or_empty(self, client: QWeatherClient, location_id: str) -> list[Hour]:
        """逐小时取不到不耽误逐日预报，结果里标 hourly_unavailable。"""

        try:
            _, hours = await self._cached(
                ("hourly", location_id), FORECAST_CACHE_SECONDS, lambda: client.hourly(location_id)
            )
        except QWeatherError:
            return []
        return hours

    async def _cached(
        self, key: tuple[str, ...], ttl: float, fetch: Callable[[], Awaitable[list[Any]]]
    ) -> tuple[datetime, list[Any]]:
        hit = self._cache.get(key, ttl)
        if hit is not None:
            return hit
        fetched_at = self._now()
        values = await fetch()
        if values:
            self._cache.put(key, fetched_at, values)
        return fetched_at, values
