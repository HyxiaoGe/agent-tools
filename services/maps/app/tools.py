"""两个工具的整套逻辑：参数校验、当前位置、地名解析、按每次上游请求先高德后腾讯、整理结果。
MCP 的说明和给模型的文字在 mcp_server.py。

2026-11-04 从 Fusion 挪到这里（Fusion app/services/mcp/amap_product_tools.py），Fusion 和灵稿共用。
和 Fusion 不一样的地方：
- 直接调两家的 Web 服务 REST，不经 amap、tmap 两个 MCP 代理（Sean 11-04 定）。高德一次搜索就有全部字段，不再逐条查详情。
- 能兜底：哪一次上游请求高德不行（额度用尽、超每秒上限、超时、连不上、出错、密钥无效），就只把这一次交给腾讯；
  上游拒了这次请求本身（参数不对、算不出路线）、查到 0 条、地名重名都不转。见 upstream.py。
- 地名按编号查（place_id）时编号带服务商前缀；那一家用不了时按名字解析。
- 地名查不到时回 place_not_found（Fusion 报的是工具不可用，模型会把它说成服务出错）。
- 路线的几种出行方式并行算；整次调用限 25 秒，到时已经算出的路线照样返回。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import urlencode

from app.model import (
    AMAP,
    COORDINATES,
    SECRET,
    TMAP,
    Endpoint,
    GeoCandidate,
    GeoLookup,
    Place,
    Point,
    Route,
    city_matches,
    keyword_terms,
    number,
)
from app.resolve import resolve_candidates, select_anywhere, select_in_city
from app.upstream import FALLBACK_KINDS, UpstreamError, Usage

logger = logging.getLogger("maps")

T = TypeVar("T")

CURRENT = "current_location"
NAMED = "named"
NONE = "none"
MODES = ("driving", "transit", "walking", "bicycling")
# 当前位置在参数里的写法：只认这两个固定值（Fusion APT:1236-1243），不是按意思判断
CURRENT_LOCATION_TEXTS = frozenset({"current_location", "当前位置"})
MAX_QUERY = 80
MAX_CITY = 40
MAX_PLACE = 120
MAX_DEPARTURE_TIME = 80
MAX_LIMIT = 5
PLACE_ID = re.compile(r"^(amap|tmap)_[A-Za-z0-9_-]{1,64}$")
# 产品经 MCP 请求的 _meta 传设备位置（WGS84，和浏览器定位一样），不走模型参数
GEOLOCATION_META_KEY = "agent-tools/geolocation"
MAX_ACCURACY_M = 50_000
APP_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_APP = "agent-tools"


class ToolFailure(Exception):
    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        place_name: str | None = None,
        argument: str | None = None,
        candidates: list[GeoCandidate] | None = None,
        reset_at: datetime | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.place_name = place_name
        self.argument = argument
        self.candidates = candidates or []
        self.reset_at = reset_at


class AllFailed(Exception):
    """每一家都没成（或都没配）。"""

    def __init__(self, errors: list[UpstreamError]):
        super().__init__(", ".join(str(e) for e in errors) or "no provider configured")
        self.errors = errors


class Client(Protocol):
    provider: str

    async def text_search(self, terms: list[str], city: str | None) -> list[Place]: ...
    async def around_search(self, terms: list[str], center: Point, radius_m: int) -> list[Place]: ...
    async def detail(self, poi_id: str) -> Place | None: ...
    async def geocode(self, label: str, city: str | None) -> GeoLookup: ...
    async def reverse_city(self, point: Point) -> str | None: ...
    async def route(self, mode: str, origin: Endpoint, destination: Endpoint) -> Route | None: ...
    async def convert_gps(self, lng: float, lat: float) -> Point: ...


@dataclass(frozen=True)
class Device:
    longitude: float
    latitude: float


def device_from_meta(meta: Any) -> Device | None:
    raw = meta.get(GEOLOCATION_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return None
    lng, lat, accuracy = number(raw.get("longitude")), number(raw.get("latitude")), number(raw.get("accuracy_m"))
    if lng is None or lat is None or not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None
    if accuracy is not None and not 0 <= accuracy <= MAX_ACCURACY_M:
        return None
    return Device(lng, lat)


def app_name(value: Any) -> str:
    """跳转链接里的来源：网关转来的调用方产品（x-caller-app）。Fusion 原来写死 src=fusion。"""
    return value if isinstance(value, str) and APP_NAME.fullmatch(value) else DEFAULT_APP


# ---------- 参数 ----------


@dataclass
class PlaceArgs:
    query: str
    anchor_source: str
    city: str | None = None
    near: str | None = None
    near_candidate: int | None = None
    radius_m: int = 3000
    limit: int = MAX_LIMIT


@dataclass
class RouteArgs:
    origin: str
    destination: str
    origin_source: str
    destination_source: str
    modes: list[str]
    origin_city: str | None = None
    destination_city: str | None = None
    origin_candidate: int | None = None
    destination_candidate: int | None = None
    origin_place_id: str | None = None
    destination_place_id: str | None = None
    requested_departure_time: str | None = None


def _invalid(detail: str) -> ToolFailure:
    return ToolFailure("invalid_arguments", detail)


def _required(name: str, value: Any, max_chars: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > max_chars:
        raise _invalid(f"{name} must be 1-{max_chars} characters")
    if SECRET.search(text):
        raise _invalid(f"{name} must not contain credentials")
    return text


def _optional(name: str, value: Any, max_chars: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _required(name, value, max_chars)


def _place_name(name: str, value: Any) -> str:
    text = _required(name, value, MAX_PLACE)
    if COORDINATES.fullmatch(text):
        raise _invalid(f"{name} must be a place name, not coordinates")
    return text


def _choice(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise _invalid(f"{name} must be a candidate number from 1 to 5")
    return value


def parse_place_args(
    *,
    query: Any,
    anchor_source: Any,
    city: Any = None,
    near: Any = None,
    near_candidate: Any = None,
    radius_m: Any = 3000,
    limit: Any = MAX_LIMIT,
) -> PlaceArgs:
    text = _required("query", query, MAX_QUERY)
    if anchor_source not in {NAMED, CURRENT, NONE}:
        raise _invalid("anchor_source must be named, current_location or none")
    near_text = _place_name("near", near) if _optional("near", near, MAX_PLACE) else None
    if (anchor_source == NAMED) != bool(near_text):
        raise _invalid("near is required when anchor_source is named, and only then")
    choice = _choice("near_candidate", near_candidate)
    if choice is not None and anchor_source != NAMED:
        raise _invalid("near_candidate is only for anchor_source=named")
    if isinstance(radius_m, bool) or not isinstance(radius_m, int) or not 100 <= radius_m <= 50_000:
        raise _invalid("radius_m must be an integer from 100 to 50000")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return PlaceArgs(
        query=text,
        anchor_source=anchor_source,
        city=_optional("city", city, MAX_CITY),
        near=near_text,
        near_candidate=choice,
        radius_m=radius_m,
        limit=limit,
    )


def _endpoint_text(name: str, value: Any, source: Any) -> str:
    text = _place_name(name, value)
    if source not in {NAMED, CURRENT}:
        raise _invalid(f"{name}_source must be named or current_location")
    is_current_text = text in CURRENT_LOCATION_TEXTS
    if (source == CURRENT) != is_current_text:
        raise _invalid(f"use {name}=current_location together with {name}_source=current_location")
    return CURRENT if is_current_text else text


def _place_id(name: str, value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not PLACE_ID.fullmatch(value.strip()):
        raise _invalid(f"{name} must be a place_id copied from an earlier place search result")
    return value.strip()


def parse_route_args(
    *,
    origin: Any,
    destination: Any,
    origin_source: Any,
    destination_source: Any,
    modes: Any,
    origin_city: Any = None,
    destination_city: Any = None,
    origin_candidate: Any = None,
    destination_candidate: Any = None,
    origin_place_id: Any = None,
    destination_place_id: Any = None,
    requested_departure_time: Any = None,
) -> RouteArgs:
    origin_text = _endpoint_text("origin", origin, origin_source)
    destination_text = _endpoint_text("destination", destination, destination_source)
    origin_choice = _choice("origin_candidate", origin_candidate)
    destination_choice = _choice("destination_candidate", destination_candidate)
    origin_id = _place_id("origin_place_id", origin_place_id)
    destination_id = _place_id("destination_place_id", destination_place_id)
    for side, source, choice, place_id in (
        ("origin", origin_source, origin_choice, origin_id),
        ("destination", destination_source, destination_choice, destination_id),
    ):
        if (choice is not None or place_id is not None) and source != NAMED:
            raise _invalid(f"{side}_candidate and {side}_place_id are only for {side}_source=named")
        # 编号已经指定了具体地点，不再和重名候选的编号同用（Fusion APT:1259-1263）
        if choice is not None and place_id is not None:
            raise _invalid(f"do not combine {side}_place_id with {side}_candidate")
    if not isinstance(modes, list) or not 1 <= len(modes) <= 3 or any(m not in MODES for m in modes):
        raise _invalid("modes must list 1-3 of driving, transit, walking, bicycling")
    if len(set(modes)) != len(modes):
        raise _invalid("modes must not repeat")
    departure = _optional("requested_departure_time", requested_departure_time, MAX_DEPARTURE_TIME)
    return RouteArgs(
        origin=origin_text,
        destination=destination_text,
        origin_source=origin_source,
        destination_source=destination_source,
        modes=list(modes),
        origin_city=_optional("origin_city", origin_city, MAX_CITY),
        destination_city=_optional("destination_city", destination_city, MAX_CITY),
        origin_candidate=origin_choice,
        destination_candidate=destination_choice,
        origin_place_id=origin_id,
        destination_place_id=destination_id,
        requested_departure_time=re.sub(r"\s+", " ", departure) if departure else None,
    )


# ---------- 结果 ----------


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None and value != []}


def amap_link(poi_id: str, app: str) -> str:
    return f"https://uri.amap.com/marker?{urlencode({'poiid': poi_id, 'src': app, 'callnative': '0'})}"


def public_place(place: Place, app: str) -> dict[str, Any]:
    # 腾讯的跳转链接要带 referer，文档说是应用名称、示例里却像 key，查不清就不给（不能把 key 放进链接）
    return _compact(
        {
            "place_id": place.place_id,
            "name": place.name,
            "address": place.address,
            "district": place.district,
            "city": place.city,
            "category": place.category,
            "distance_m": place.distance_m,
            "rating": place.rating,
            "reference_cost_yuan": place.reference_cost_yuan,
            "open_hours": place.open_hours,
            "business_area": place.business_area,
            "photo_url": place.photo_url,
            "map_url": amap_link(place.id, app) if place.provider == AMAP else None,
            "provider": place.provider,
        }
    )


def public_endpoint(endpoint: Endpoint) -> dict[str, Any]:
    return _compact({"source": endpoint.source, "label": endpoint.label, "city": endpoint.city})


def _public_legs(route: Route) -> list[dict[str, Any]]:
    return [_compact(vars(leg).copy()) for leg in route.legs]


def public_route(route: Route, provider: str | None = None) -> dict[str, Any]:
    data = {
        "mode": route.mode,
        "distance_m": route.distance_m,
        "duration_s": route.duration_s,
        "summary": route.summary,
        "toll_yuan": route.toll_yuan,
        "fare_yuan": route.fare_yuan,
        "transit_type": route.transit_type,
        "transfers": route.transfers,
        "walking_distance_m": route.walking_distance_m,
        "legs": _public_legs(route),
        "alternatives": [
            _compact(
                {
                    "transit_type": alt.transit_type,
                    "duration_s": alt.duration_s,
                    "walking_distance_m": alt.walking_distance_m,
                    "transfers": alt.transfers,
                    "fare_yuan": alt.fare_yuan,
                    "summary": alt.summary,
                    "legs": _public_legs(alt),
                }
            )
            for alt in route.alternatives
        ],
        "provider": provider,
    }
    return _compact(data)


def public_candidate(index: int, candidate: GeoCandidate) -> dict[str, Any]:
    """重名候选给模型判断是不是同一处；坐标只给它自己判断，说明里写明不能给用户看（Fusion 30efef77）。"""
    return _compact(
        {
            "candidate": index,
            "city": candidate.city,
            "district": candidate.district,
            "level": candidate.level,
            "category": candidate.category,
            "location": candidate.location.text(),
        }
    )


# ---------- 编排 ----------


@dataclass
class _Outcome:
    mode: str
    route: Route | None = None
    provider: str | None = None
    reason: str | None = None  # rejected / no_route / quota / unavailable / timeout
    reset_at: datetime | None = None


@dataclass
class _Call:
    """一次工具调用：记下用到了哪几家。"""

    clients: list[Client]
    usage: Usage
    used: set[str] = field(default_factory=set)

    def client(self, provider: str) -> Client | None:
        return next((c for c in self.clients if c.provider == provider), None)

    async def first(self, op: Callable[[Client], Awaitable[T]]) -> tuple[str, T]:
        """先高德后腾讯：这一家可以转的失败就交给下一家；上游拒了这次请求（rejected）照样抛出。"""
        errors: list[UpstreamError] = []
        for client in self.clients:
            if errors:
                self.usage.fallback()
            try:
                value = await op(client)
            except UpstreamError as error:
                if error.kind not in FALLBACK_KINDS:
                    self.used.add(client.provider)
                    raise
                errors.append(error)
                if error.kind != "unsupported":
                    logger.info("maps_fallback provider=%s api=%s kind=%s", error.provider, error.api, error.kind)
                continue
            self.used.add(client.provider)
            return client.provider, value
        raise AllFailed(errors)

    async def device_point(self, device: Device | None) -> Point:
        if device is None:
            raise ToolFailure("location_unavailable")
        _, point = await self.first(lambda c: c.convert_gps(device.longitude, device.latitude))
        return point

    async def reverse_city(self, point: Point) -> str | None:
        """当前位置的城市只用来给高德算公交；拿不到也接着算（Fusion APT:783-798）。"""
        try:
            _, city = await self.first(lambda c: c.reverse_city(point))
        except (UpstreamError, AllFailed):
            return None
        return city

    async def resolve_named(
        self, label: str, *, city: str | None, preferred_city: str | None, choice: int | None, argument: str
    ) -> Endpoint:
        try:
            _, lookup = await self.first(lambda c: c.geocode(label, city))
        except UpstreamError:
            lookup = GeoLookup([])  # 高德地址解析查不到时回 30001，当没解析出来，接着按名称搜
        resolution = resolve_candidates(
            lookup.candidates, label=label, requested_city=city, preferred_city=preferred_city, choice=choice
        )
        if resolution.candidates:
            raise ToolFailure(
                "ambiguous_location", place_name=label, argument=argument, candidates=resolution.candidates
            )
        if resolution.endpoint is not None:
            return resolution.endpoint
        selection_city = city or preferred_city
        if lookup.places is not None:
            places = lookup.places
        else:
            try:
                _, places = await self.first(lambda c: c.text_search(keyword_terms(label), selection_city))
            except UpstreamError:
                places = []
        endpoint = (
            select_in_city(places, label=label, city=selection_city)
            if selection_city
            else select_anywhere(places, label=label)
        )
        if endpoint is None:
            raise ToolFailure("place_not_found", place_name=label)
        return endpoint

    async def by_place_id(self, label: str, place_id: str, city: str | None, argument: str) -> Endpoint:
        provider, _, poi_id = place_id.partition("_")
        client = self.client(provider)
        if client is not None:
            try:
                place = await client.detail(poi_id)
            except UpstreamError as error:
                self.used.add(provider)
                if error.kind not in FALLBACK_KINDS:
                    raise ToolFailure("place_id_unresolved", place_name=label) from None
                logger.info("maps_place_id_by_name provider=%s kind=%s", provider, error.kind)
            else:
                self.used.add(provider)
                endpoint = _endpoint_from_place(place, label, city)
                if endpoint is None:
                    raise ToolFailure("place_id_unresolved", place_name=label)
                return endpoint
        # 编号是哪家的，那家现在用不了（没配、额度用尽、出错）：按名字解析。上游失败不说成编号无效（Fusion afa9494e）
        try:
            return await self.resolve_named(label, city=city, preferred_city=None, choice=None, argument=argument)
        except ToolFailure as failure:
            if failure.code == "ambiguous_location":
                # 模型给的是编号，再让它带候选号重查会和编号冲突，这里按查不了处理
                raise ToolFailure("unavailable") from None
            raise

    async def endpoint(
        self,
        label: str,
        place_id: str | None,
        city: str | None,
        preferred_city: str | None,
        choice: int | None,
        argument: str,
    ) -> Endpoint:
        if place_id:
            return await self.by_place_id(label, place_id, city, argument)
        return await self.resolve_named(
            label, city=city, preferred_city=preferred_city, choice=choice, argument=argument
        )

    async def route(self, mode: str, origin: Endpoint, destination: Endpoint) -> _Outcome:
        try:
            provider, route = await self.first(lambda c: c.route(mode, origin, destination))
        except UpstreamError:
            return _Outcome(mode, reason="rejected")
        except AllFailed as failed:
            failure = _failure_of(failed)
            return _Outcome(
                mode, reason="quota" if failure.code == "quota_exhausted" else "unavailable", reset_at=failure.reset_at
            )
        if route is None:
            return _Outcome(mode, reason="no_route")
        return _Outcome(mode, route=route, provider=provider)


def _endpoint_from_place(place: Place | None, label: str, city: str | None) -> Endpoint | None:
    """按编号查到的地点：要有坐标；给了城市要对得上；没给城市时地点自己要有城市（Fusion APT:1535-1562）。"""
    if place is None or place.location is None:
        return None
    if city and place.city and not city_matches(city, place.city):
        return None
    if not city and not place.city:
        return None
    return Endpoint(label, place.location, place.city or city)


def _failure_of(failed: AllFailed) -> ToolFailure:
    """每一家都没成：都是额度用尽就回 quota_exhausted，带最早恢复的时间；否则 unavailable。"""
    errors = [e for e in failed.errors if e.kind != "unsupported"]
    if errors and all(e.kind == "quota" for e in errors):
        resets = [e.reset_at for e in errors if e.reset_at is not None]
        return ToolFailure("quota_exhausted", reset_at=min(resets) if resets else None)
    return ToolFailure("unavailable")


class MapsService:
    def __init__(self, clients: list[Client], usage: Usage, *, call_timeout: float = 25.0):
        self.clients = clients
        self.usage = usage
        self.call_timeout = call_timeout

    @property
    def providers(self) -> list[str]:
        return [client.provider for client in self.clients]

    async def place_search(self, args: PlaceArgs, device: Device | None = None, app: str = DEFAULT_APP) -> dict:
        call = _Call(self.clients, self.usage)
        try:
            async with asyncio.timeout(self.call_timeout):
                return await self._place_search(call, args, device, app)
        except TimeoutError:
            raise ToolFailure("timeout") from None
        except AllFailed as failed:
            raise _failure_of(failed) from None
        except UpstreamError:
            raise ToolFailure("unavailable") from None

    async def _place_search(self, call: _Call, args: PlaceArgs, device: Device | None, app: str) -> dict:
        terms = keyword_terms(args.query)
        anchor: Endpoint | None = None
        if args.anchor_source == CURRENT:
            anchor = Endpoint(None, await call.device_point(device), None, source=CURRENT)
        elif args.anchor_source == NAMED and args.near:
            anchor = await call.resolve_named(
                args.near, city=args.city, preferred_city=None, choice=args.near_candidate, argument="near_candidate"
            )
        if anchor is not None:
            center = anchor.location
            provider, places = await call.first(lambda c: c.around_search(terms, center, args.radius_m))
        else:
            provider, places = await call.first(lambda c: c.text_search(terms, args.city))
        places = places[: args.limit]
        limitations = []
        if provider == TMAP and len(terms) > 1:
            limitations.append("first_term_only")
        result: dict[str, Any] = {
            "ok": True,
            "status": "success",
            "query": args.query,
            "city": args.city,
            "near": args.near,
            "anchor": public_endpoint(anchor) if anchor else None,
            "result_count": len(places),
            "places": [public_place(place, app) for place in places],
            "limitations": limitations,
            "providers": sorted(call.used),
        }
        return {key: value for key, value in result.items() if value is not None}

    async def route_compare(self, args: RouteArgs, device: Device | None = None) -> dict:
        call = _Call(self.clients, self.usage)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.call_timeout
        try:
            async with asyncio.timeout_at(deadline):
                origin, destination = await self._endpoints(call, args, device)
        except TimeoutError:
            raise ToolFailure("timeout") from None
        except AllFailed as failed:
            raise _failure_of(failed) from None
        except UpstreamError:
            raise ToolFailure("unavailable") from None

        tasks = [asyncio.create_task(call.route(mode, origin, destination)) for mode in args.modes]
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        outcomes = [
            task.result() if task in done else _Outcome(mode, reason="timeout")
            for mode, task in zip(args.modes, tasks, strict=True)
        ]
        routes = [o for o in outcomes if o.route is not None]
        failed = [o for o in outcomes if o.route is None]
        if not routes:
            reasons = {o.reason for o in failed}
            if reasons == {"quota"}:
                resets = [o.reset_at for o in failed if o.reset_at is not None]
                raise ToolFailure("quota_exhausted", reset_at=min(resets) if resets else None)
            if reasons <= {"rejected", "no_route"}:
                raise ToolFailure("no_route")
            if reasons == {"timeout"}:
                raise ToolFailure("timeout")
            raise ToolFailure("unavailable")
        limitations = ["departure_time_not_applied"] if args.requested_departure_time else []
        result: dict[str, Any] = {
            "ok": True,
            "status": "degraded" if failed else "success",
            "origin": public_endpoint(origin),
            "destination": public_endpoint(destination),
            "requested_departure_time": args.requested_departure_time,
            "routes": [public_route(o.route, o.provider) for o in routes if o.route is not None],
            "unavailable_modes": [o.mode for o in failed],
            "limitations": limitations,
            "providers": sorted(call.used),
        }
        return {key: value for key, value in result.items() if value is not None}

    async def _endpoints(self, call: _Call, args: RouteArgs, device: Device | None) -> tuple[Endpoint, Endpoint]:
        current: Endpoint | None = None
        if CURRENT in (args.origin_source, args.destination_source):
            point = await call.device_point(device)
            current = Endpoint(None, point, await call.reverse_city(point), source=CURRENT)
        if args.origin_source == CURRENT and current is not None:
            origin = current
        else:
            origin = await call.endpoint(
                args.origin, args.origin_place_id, args.origin_city, None, args.origin_candidate, "origin_candidate"
            )
        if args.destination_source == CURRENT and current is not None:
            destination = current
        else:
            destination = await call.endpoint(
                args.destination,
                args.destination_place_id,
                args.destination_city,
                origin.city if not args.destination_city else None,
                args.destination_candidate,
                "destination_candidate",
            )
        return origin, destination
