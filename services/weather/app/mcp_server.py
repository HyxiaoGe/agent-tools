"""MCP 工具 get_weather：产品经网关 /weather/mcp 给自己的模型用，逻辑见 app/forecast.py。

说明（英文，模型看的）写在这里，两个产品共用；产品专有的话（结果怎么显示、查不到时用哪个工具）由产品接在后面，
所以这里不点名别的工具、不提卡片、不写工具自己的名字（产品可能改名）。规则来自 Fusion 和灵稿各自踩过的坑：
省份改查省会（Fusion 10-13）、逐小时摘要可以用（10-08）、失败时不让模型把工具状态讲给用户（9-19）等。

结果：structuredContent 是整份（给页面画卡片），文字是去掉逐小时明细的 JSON（给模型）。
失败：structuredContent 是 {ok:false, error:{code, message, retryable:false, candidates?}}，文字是给模型的说明；
isError 保持 false，和 image 的 MCP 一样，客户端读 structuredContent。retryable 一律 false：同样的参数再查也不会好，
该换别的来源（Fusion 9-19：覆盖不到的地区被当成临时故障，每次多烧几轮才转去搜索）。
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from app.forecast import FORECAST_DAYS, MAX_LOCATION_LENGTH, Forecaster, ForecastError, parse_arguments

logger = logging.getLogger("weather")

TOOL_NAME = "get_weather"

DESCRIPTION = f"""Get the weather forecast (QWeather) for a named place: a daily forecast for up to {FORECAST_DAYS} days starting from the place's local today, and a summary of the coming hours when the request covers today, tomorrow, or no specific date.

Places: forecasts are available for cities and districts or counties, not for whole provinces, scenic spots, streets, or buildings. For a place inside a city (e.g. West Lake), pass the complete place text as the user wrote it and do not add a city the user did not provide. When the user asks about a whole province or region (e.g. Hunan), query its capital or main city instead (e.g. Changsha) and say in the answer which city it represents. Call only for a place the user affirmatively wants checked, never for a negated, excluded, or explicitly unwanted place.

Dates: when the user asks about one specific day, pass it as requested_date. Resolve relative wording such as tomorrow, the day after tomorrow, or this Saturday to the exact date from the current date you were given. Pass it even if it may be outside the forecast window. Omit requested_date when the day is unclear (such as "the next few days"), when several days are asked about, or for negated or excluded dates.

The result is JSON:
- resolved_location: the administrative area the forecast covers; do not describe it as weather for a specific building or street.
- local_today: the current date at the place.
- forecast_days: date, weekday, day_weather and night_weather, high_c and low_c in °C, and day and night wind direction and wind power. When requested_date falls within the forecast window, only that day is returned; otherwise the whole window is returned.
- hourly_summary, when present: covers only its window within the next 24 hours, limited to the requested date's hours when one day is returned. It has window; temp_min_c and temp_max_c; max_pop_percent and max_pop_time, the peak precipitation probability and its time; precipitation_hours, the number of hours with precipitation; first_precipitation_time and last_precipitation_time; and total_precip_mm. The last three are omitted when no precipitation is forecast. Its times carry the place's local UTC offset; state them as local clock times.
- limitations: requested_date_not_covered means the requested date is not in forecast_days; fewer_days means fewer than {FORECAST_DAYS} days were available; hourly_unavailable means the hourly data could not be fetched.
- fetched_at: when the forecast was fetched, in China Standard Time.

Using the result:
- Refer only to dates, conditions, temperatures, wind, and hourly_summary values actually returned.
- The daily forecast gives only daytime and nighttime conditions; a question about a specific time of day can be answered more precisely only from hourly_summary within its window, otherwise say the daily granularity cannot confirm it. A condition such as "cloudy" does not prove that rain, thunder, snow, or strong wind is absent; absence must come from returned fields.
- If the requested date is not covered and no authoritative source gives that exact date's forecast, state only that its weather is unavailable and give the returned forecast range. Do not add other days' weather or historical climate statistics.
- Do not add current temperature, humidity, air quality, alerts, flooding, congestion, or delays. Mention precipitation probability or amount only as returned in hourly_summary.
- Practical advice such as carrying an umbrella or whether conditions suit an activity should follow from the returned forecast.
- When the user asks about one date and one condition, answer briefly without recapping adjacent days or unrelated metrics.
- For what this tool cannot cover, such as dates beyond the forecast, past weather, current conditions, humidity, air quality, or alerts, use a web search tool if one is available to find authoritative sources, with citations and coverage limits.

On failure the result has ok=false and says what to do next. In every case, do not fabricate weather facts."""

LOCATION_DESCRIPTION = (
    "The complete place text the user supplied, such as 广州, 杭州西湖区, or Paris; for a whole province or region, "
    f"its capital or main city. A place name of 1-{MAX_LOCATION_LENGTH} characters, not coordinates or an "
    "administrative code."
)
REQUESTED_DATE_DESCRIPTION = (
    "One specific day the user asks about, in YYYY-MM-DD form, with relative wording resolved to the exact date. "
    "Omit it when the day is unclear, when several days are asked about, or for negated or excluded dates."
)

# 给日志和页面看的原因；模型看的是下面 _model_text 的说明
MESSAGES = {
    "location_not_found": "The weather service could not find this place.",
    "ambiguous_location": "The place name matches several areas.",
    "not_configured": "The weather service is not configured.",
    "upstream_timeout": "QWeather did not respond in time.",
    "upstream_unavailable": "QWeather returned an error.",
    "upstream_rate_limited": "QWeather rate-limited the request.",
    "upstream_auth_failed": "QWeather rejected the service credentials.",
    "invalid_response": "QWeather returned no usable forecast.",
    "call_timeout": "The forecast took too long.",
}
UNAVAILABLE = (
    "The weather service returned no usable result. Answer from available information and do not fabricate "
    "weather facts."
)


def _model_text(error: ForecastError) -> str:
    if error.code == "ambiguous_location":
        candidates = json.dumps(error.candidates, ensure_ascii=False)
        return (
            f"No forecast was fetched: the place name matches several areas. Candidates: {candidates}. If the "
            "conversation already makes clear which one the user means, call this tool again with location set "
            "exactly to that candidate text. Otherwise ask the user which one they mean and list the candidates in "
            "plain words. Do not guess, and do not fabricate weather facts."
        )
    if error.code == "location_not_found":
        return (
            "No forecast was fetched: the weather service could not find this place. Forecasts are available for "
            "cities and districts or counties, not for scenic spots, streets, or buildings. If the place clearly "
            "belongs to a city or district known from the conversation, call this tool again with that city or "
            "district; otherwise tell the user the place was not found and ask for its city. Do not fabricate "
            "weather facts."
        )
    if error.code == "invalid_arguments":
        return (
            f"No forecast was fetched: {error.detail}. Call again with corrected arguments only if the user's "
            "request supports it, and do not fabricate weather facts."
        )
    # 其余都是服务这边的问题（超时、和风出错、没配置）：只告诉模型没有可用结果，不让它把工具状态讲给用户
    return UNAVAILABLE


def failure(error: ForecastError) -> ToolResult:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.detail or MESSAGES.get(error.code, "The forecast failed."),
        "retryable": False,
    }
    if error.candidates:
        body["candidates"] = error.candidates
    return ToolResult(content=_model_text(error), structured_content={"ok": False, "error": body})


def success(result: dict[str, Any]) -> ToolResult:
    """模型看去掉逐小时明细的那份：24 条明细它容易漏看降水高峰，也容易逐条复述。"""

    seen = {key: value for key, value in result.items() if key != "hourly"}
    return ToolResult(content=json.dumps(seen, ensure_ascii=False, separators=(",", ":")), structured_content=result)


def build_mcp(forecaster: Forecaster) -> FastMCP:
    mcp = FastMCP(
        "weather",
        instructions="get_weather returns a QWeather forecast for a named place; see the tool description.",
    )

    @mcp.tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
        output_schema=None,
    )
    async def get_weather(
        location: Annotated[str, Field(description=LOCATION_DESCRIPTION)],
        requested_date: Annotated[str | None, Field(description=REQUESTED_DATE_DESCRIPTION)] = None,
    ) -> ToolResult:
        try:
            place, on = parse_arguments(location, requested_date)
            result = await forecaster.forecast(place, on)
        except ForecastError as error:
            # 地名、日期不进日志
            logger.info("get_weather_failed code=%s", error.code)
            return failure(error)
        return success(result)

    return mcp
