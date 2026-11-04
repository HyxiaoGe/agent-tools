"""MCP 工具 place_search、route_compare：产品经网关 /maps/mcp 给自己的模型用，逻辑见 app/tools.py。

说明（英文，模型看的）写在这里，两个产品共用，以 Fusion 的说明为底（runtime_prompts.toml 的 [amap]），带上了这些：
用户限定了区就每次搜索都带上、没有就如实说（Fusion 16df25f5）；不给坐标；重名的四种情况（Fusion c3bc81cb、c1bf8a0a）；
出发时间只记用户原话、不按那个时刻算（8137c527）；公交不给总距离。
没带的：Fusion 的“公交没有票价”（这里取了 fare_yuan）；Fusion 收尾那几条（同类返回值可以直接算差、不讲停车和实时拥堵、limitations 里的事不暗示，TOML:290-292）。
“中国大陆”“评分满分 5”“驾车用时按查询时路况”“人均消费”这些是这里新写的，不是 Fusion 的说明。
产品专有的话由产品接在后面：回答风格（不用表格、不提服务商名字）、结果怎么显示、和别的工具怎么配合（Fusion 的航班、火车站换乘那段）。
所以这里不点名别的工具、不提卡片、不写工具自己的名字（产品可能改名）。

结果：structuredContent 是整份（给页面画卡片），文字是给模型的那份 JSON（去掉图片、跳转链接和数据来源，免得它讲给用户）。
失败：structuredContent 是 {ok:false, error:{code, message, retryable, ...}}，文字是给模型的说明；isError 保持 false，和 weather、image 一样。
重名 retryable=true（带上候选号再查就行），其余都是 false。
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from app.tools import (
    MAX_LIMIT,
    MapsService,
    ToolFailure,
    app_name,
    device_from_meta,
    parse_place_args,
    parse_route_args,
    public_candidate,
)

logger = logging.getLogger("maps")

PLACE_TOOL = "place_search"
ROUTE_TOOL = "route_compare"

PLACE_DESCRIPTION = """Search for places such as restaurants, shops, attractions and facilities in mainland China: within a city, near a named place, or near the device's current location.

Arguments:
- anchor_source is required and is never inferred: named searches near the place given in near; current_location searches near the device's current location; none searches by text within city (nationwide when city is omitted). near accepts only a place name, never coordinates.
- When the user limits results to a named district, include that district in every search query for this request, including follow-up searches for another category; a city-wide query does not establish district membership. If no returned place matches the requested district and category, report that gap instead of expanding to another district or guessing where shops may be concentrated.
- Separate alternative search terms with | or commas; spaces remain part of the query rather than meaning OR.

The result is JSON:
- places: each has place_id and name, and when available address, district, city, category, distance_m (from the anchor), rating (out of 5), reference_cost_yuan (the average spending per person listed by the map provider, not a real-time price), open_hours and business_area. A place may lack fields another place has.
- anchor: the resolved anchor when anchor_source is named or current_location.
- limitations: first_term_only means only the first alternative search term was searched.

Using the result:
- Refer only to places actually returned and fields actually returned for them; do not introduce a place absent from places. When a field the answer depends on is missing, say this query's results cannot confirm it; do not guess or fill it in.
- Do not infer real-time queues, availability, reservations, walking time between places, or distance between places. Do not describe places as adjacent, on the way, or near each other unless returned fields show it, and do not infer attributes such as taste or merchant density from a place name or address.
- Discuss a place's distance only when it returned distance_m, and only as the distance from this query's anchor, never between places.
- Keep comparisons and superlatives within the returned places; do not turn them into citywide or general claims.
- To get a route to or from a returned place, use its place_id together with its returned name.

On failure the result has ok=false and says what to do next. In every case, do not fabricate place facts."""

ROUTE_DESCRIPTION = """Compare up to three transport modes (driving, transit, walking, bicycling) between two places in mainland China. Origin and destination are natural-language place names resolved by the map service; never pass coordinates.

Arguments:
- Every call must set origin_source, destination_source and modes explicitly; they are never inferred. For a named place, pass the place text the user supplied and set the source to named. For the device's current location, pass current_location as the place text and set the source to current_location.
- City fields are optional. When the user names two places, call even if the city is not explicit: a destination without a city is matched in the origin's city first. Do not search the web first to guess a city.
- When an earlier place search result in this conversation already returned the exact place the user means as an endpoint, pass its place_id as origin_place_id or destination_place_id and its returned name as origin or destination, so that exact place is used instead of resolving the name again.
- When the user specifies a date or time, pass requested_departure_time. It records the user's words only; routes are not calculated for that time. Omit it when unspecified and do not default it to the present.

The result is JSON:
- origin and destination: the resolved endpoints with their cities when known.
- routes: one per mode that could be computed, with duration_s; distance_m except for transit; for driving, summary (the routing strategy) and toll_yuan; for transit, transit_type (subway, bus, mixed or public_transit), transfers, walking_distance_m, fare_yuan, legs (kind, line_name, departure_stop, arrival_stop, via_stop_count, entrance, exit, distance_m, duration_s) and up to two alternatives.
- unavailable_modes: requested modes that could not be computed.
- limitations: departure_time_not_applied means requested_departure_time was not used to calculate the routes.

Using the result:
- Refer only to routes and fields actually returned; do not introduce an absent route or transport mode. When a field the answer depends on is missing, say this query's results cannot confirm it; do not guess or fill it in.
- Driving durations are estimated from traffic at the time of this query. Do not estimate route time, distance, fares or tolls beyond returned values. For transit use only the returned transit_type, transfers, legs, fare_yuan and alternatives; do not guess missing lines, stops, entrances, exits or walking distances. Transit has no total distance; do not derive one.
- fare_yuan is the reference fare the map data gives for the plan; do not present it as exact or apply discounts.
- When requested_departure_time was passed, clearly state that the routes were calculated for the time of this query, not for that time, and do not infer an arrival time.

On failure the result has ok=false and says what to do next. In every case, do not fabricate route facts."""

QUERY = "What to search for, such as 火锅 or 咖啡|奶茶, including a district the user named. 1-80 characters."
ANCHOR_SOURCE = "named searches near the place in near, current_location searches near the device's current location, none searches by text within city."
CITY = "City to search in, such as 上海. Optional; for anchor_source=named it also narrows which place near refers to."
NEAR = "Place name to search near, as the user supplied it. Required when anchor_source=named, otherwise omit. Never coordinates."
CANDIDATE = "Only after a result reported ambiguous_location for this place name: the candidate number from that result's candidates. Omit otherwise."
RADIUS = "Search radius in meters around the anchor, 100-50000. Default 3000."
LIMIT = f"Maximum number of places to return, 1-{MAX_LIMIT}. Default {MAX_LIMIT}."
ORIGIN = "Named origin as the user supplied it, or current_location for the device's current location."
DESTINATION = "Named destination as the user supplied it, or current_location for the device's current location."
SOURCE = "named for a place name, current_location for the device's current location."
ENDPOINT_CITY = "City of this endpoint when the user gave it or it is known from the conversation. Optional."
PLACE_ID = "Optional. The place_id of the exact place for this endpoint, copied from an earlier place search result in this conversation, with the place's returned name as the endpoint text. Never invent or modify an id, and do not combine it with a candidate number for the same endpoint."
MODES = "1-3 distinct transport modes to compare, from driving, transit, walking and bicycling."
DEPARTURE = "Date or departure time in the user's own words, such as 'weekday 08:30'. Only when the user specified one; it is not used to calculate the routes."

# 给日志和页面看的原因；模型看的是 _model_text 的说明
MESSAGES = {
    "invalid_arguments": "The arguments are invalid.",
    "ambiguous_location": "The place name matches several locations.",
    "place_not_found": "The place could not be found.",
    "place_id_unresolved": "The place id could not be resolved.",
    "location_unavailable": "The device location was not provided.",
    "no_route": "No route could be computed for the requested modes.",
    "quota_exhausted": "The map providers' quota is used up.",
    "unavailable": "The map providers returned no usable result.",
    "timeout": "The map query took too long.",
}
NO_RESULT = "The map service returned no usable result. Answer from available information and do not fabricate place or route facts."


def _ambiguous_text(error: ToolFailure, tool: str) -> str:
    details = json.dumps(
        {
            "place_name": error.place_name,
            "candidate_argument": error.argument,
            "candidates": [public_candidate(i, c) for i, c in enumerate(error.candidates, start=1)],
        },
        ensure_ascii=False,
    )
    route_case = (
        "- An earlier place search result in this conversation already returned the exact place the user means: call once more with that place's place_id as the place id argument for this endpoint instead of a candidate number.\n"
        if tool == ROUTE_TOOL
        else ""
    )
    return (
        f"No result was fetched: the place name matches several locations, so none was picked. Details: {details}. "
        "Candidates are in the map data's order, each with a candidate number, city, district, match level or category, and coordinates. Decide which case applies:\n"
        f"{route_case}"
        "- The candidates are one place: entrances or buildings of the same complex, mall or station, segments of the same street, or one specific match next to a candidate whose level is only a city or district: call once more with the same arguments and set the argument named in candidate_argument to the number of the specific candidate for that place.\n"
        "- The user's message already says which one is meant (it names the district, a nearby landmark or a more specific address): call once more with the same arguments and set that argument to the matching candidate number.\n"
        "- Otherwise the candidates are different places, or you are not sure: do not choose for the user, even if one looks more famous. Reply without calling any other tool, including web search: briefly explain that this name refers to several places, describe each by its city, district and kind, and ask which one is meant or for a more specific address.\n"
        "Coordinates are only for your own judgment; never show them to the user. Do not guess a location, compute or look up anything for a guessed location, or fabricate place facts."
    )


def _model_text(error: ToolFailure, tool: str) -> str:
    if error.code == "ambiguous_location":
        return _ambiguous_text(error, tool)
    if error.code == "invalid_arguments":
        return (
            f"No result was fetched: {error.detail}. Call again with corrected arguments only if the user's request "
            "supports it, and do not fabricate place or route facts."
        )
    if error.code == "place_not_found":
        return (
            f"No result was fetched: the map service could not find the place {json.dumps(error.place_name, ensure_ascii=False)}. "
            "If the conversation makes its city or a more specific name clear, call again with it; otherwise tell the "
            "user the place was not found and ask for its city or a more specific name. Do not guess a location or "
            "fabricate place or route facts."
        )
    if error.code == "place_id_unresolved":
        return (
            "No route was fetched: the place_id passed for an endpoint could not be resolved, and the same id will not "
            "resolve on another call. A place_id returned for that place by an earlier place search result can be used "
            "instead; without one, give the endpoint by place name. Do not fabricate place or route facts."
        )
    if error.code == "location_unavailable":
        return (
            "No result was fetched: the device's current location is not available. Do not claim to know where the "
            "user is. Ask the user for a place name or address to use instead, then call again with it as a named "
            "place."
        )
    if error.code == "no_route":
        return (
            "No route could be computed between these places for the requested modes, for example because they are "
            "too far apart for walking or cycling or no transit connects them. Tell the user this; do not estimate a "
            "route or fabricate route facts."
        )
    # 额度用尽、超时、上游出错：只告诉模型没有可用结果，不让它把工具状态讲给用户
    return NO_RESULT


def failure(error: ToolFailure, tool: str) -> ToolResult:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.detail or MESSAGES.get(error.code, "The map query failed."),
        "retryable": error.code == "ambiguous_location",
    }
    if error.place_name:
        body["place_name"] = error.place_name
    if error.code == "ambiguous_location":
        body["candidate_argument"] = error.argument
        body["candidates"] = [public_candidate(i, c) for i, c in enumerate(error.candidates, start=1)]
    if error.reset_at is not None:
        body["reset_at"] = error.reset_at.isoformat()
    return ToolResult(content=_model_text(error, tool), structured_content={"ok": False, "error": body})


# 模型看不到的字段：图片、跳转链接、数据来源（卡片上有，模型不用讲）
_HIDDEN = {"ok", "photo_url", "map_url", "provider", "providers"}


def _for_model(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _for_model(item) for key, item in value.items() if key not in _HIDDEN}
    if isinstance(value, list):
        return [_for_model(item) for item in value]
    return value


def success(result: dict[str, Any]) -> ToolResult:
    text = json.dumps(_for_model(result), ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=text, structured_content=result)


def _request_meta(ctx: Context | None) -> Any:
    request = ctx.request_context if ctx is not None else None
    return request.meta if request is not None else None


def build_mcp(service: MapsService) -> FastMCP:
    mcp = FastMCP(
        "maps",
        instructions="place_search finds places and route_compare compares transport modes in mainland China; see the tool descriptions.",
    )
    annotations = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

    @mcp.tool(name=PLACE_TOOL, description=PLACE_DESCRIPTION, annotations=annotations, output_schema=None)
    async def place_search(
        query: Annotated[str, Field(description=QUERY)],
        anchor_source: Annotated[Literal["named", "current_location", "none"], Field(description=ANCHOR_SOURCE)],
        city: Annotated[str | None, Field(description=CITY)] = None,
        near: Annotated[str | None, Field(description=NEAR)] = None,
        near_candidate: Annotated[int | None, Field(description=CANDIDATE, ge=1, le=5)] = None,
        radius_m: Annotated[int, Field(description=RADIUS, ge=100, le=50_000)] = 3000,
        limit: Annotated[int, Field(description=LIMIT, ge=1, le=MAX_LIMIT)] = MAX_LIMIT,
        ctx: Context | None = None,
    ) -> ToolResult:
        try:
            args = parse_place_args(
                query=query,
                anchor_source=anchor_source,
                city=city,
                near=near,
                near_candidate=near_candidate,
                radius_m=radius_m,
                limit=limit,
            )
            app = app_name(get_http_headers().get("x-caller-app"))
            result = await service.place_search(args, device_from_meta(_request_meta(ctx)), app)
        except ToolFailure as error:
            # 地名、关键词不进日志
            logger.info("place_search_failed code=%s", error.code)
            return failure(error, PLACE_TOOL)
        except Exception:
            # 代码自己的错：留崩溃栈，给模型的只说查不到（和 reader 一样，不把异常文字交给模型）
            logger.exception("place_search_crashed")
            return failure(ToolFailure("unavailable"), PLACE_TOOL)
        return success(result)

    @mcp.tool(name=ROUTE_TOOL, description=ROUTE_DESCRIPTION, annotations=annotations, output_schema=None)
    async def route_compare(
        origin: Annotated[str, Field(description=ORIGIN)],
        destination: Annotated[str, Field(description=DESTINATION)],
        origin_source: Annotated[Literal["named", "current_location"], Field(description=SOURCE)],
        destination_source: Annotated[Literal["named", "current_location"], Field(description=SOURCE)],
        modes: Annotated[
            list[Literal["driving", "transit", "walking", "bicycling"]],
            Field(description=MODES, min_length=1, max_length=3),
        ],
        origin_city: Annotated[str | None, Field(description=ENDPOINT_CITY)] = None,
        destination_city: Annotated[str | None, Field(description=ENDPOINT_CITY)] = None,
        origin_candidate: Annotated[int | None, Field(description=CANDIDATE, ge=1, le=5)] = None,
        destination_candidate: Annotated[int | None, Field(description=CANDIDATE, ge=1, le=5)] = None,
        origin_place_id: Annotated[str | None, Field(description=PLACE_ID)] = None,
        destination_place_id: Annotated[str | None, Field(description=PLACE_ID)] = None,
        requested_departure_time: Annotated[str | None, Field(description=DEPARTURE)] = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        try:
            args = parse_route_args(
                origin=origin,
                destination=destination,
                origin_source=origin_source,
                destination_source=destination_source,
                modes=modes,
                origin_city=origin_city,
                destination_city=destination_city,
                origin_candidate=origin_candidate,
                destination_candidate=destination_candidate,
                origin_place_id=origin_place_id,
                destination_place_id=destination_place_id,
                requested_departure_time=requested_departure_time,
            )
            result = await service.route_compare(args, device_from_meta(_request_meta(ctx)))
        except ToolFailure as error:
            logger.info("route_compare_failed code=%s", error.code)
            return failure(error, ROUTE_TOOL)
        except Exception:
            logger.exception("route_compare_crashed")
            return failure(ToolFailure("unavailable"), ROUTE_TOOL)
        return success(result)

    return mcp
