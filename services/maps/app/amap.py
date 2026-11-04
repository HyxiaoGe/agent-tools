"""高德 Web 服务（restapi.amap.com）。只请求这里列出的固定路径，不跟随重定向、不重试；key 只拼在请求参数里，不进日志。

搜索用 v5 加 show_fields=business,photos：一次请求就有坐标、区、评分、参考消费、营业时间和图片。
高德 MCP 的搜索结果是删减过的，Fusion 为补字段对前 3 条逐条查详情，一次搜索占 4 次额度（11-04 样本对比过）。
返回 status=1 才算成功；失败看 infocode。额度是按接口组计的月配额，错误码虽然叫 DAILY，北京时间每月 1 日零点恢复
（Fusion a70142d9 → 35a0b82c）；关键字搜索、周边搜索、详情共用一组。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.model import (
    AMAP,
    MAX_ALTERNATIVES,
    MAX_LEGS,
    Endpoint,
    GeoCandidate,
    GeoLookup,
    Leg,
    Place,
    Point,
    Route,
    amount,
    finish_transit,
    is_subway,
    non_negative_int,
    official_media_url,
    rating,
    text,
)
from app.upstream import BEIJING, Gate, UpstreamError

BASE_URL = "https://restapi.amap.com"
SEARCH_FIELDS = "business,photos"
PAGE_SIZE = 10
MEDIA_DOMAINS = ("amap.com", "autonavi.com")
ROUTE_PATHS = {
    "driving": "/v3/direction/driving",
    "walking": "/v3/direction/walking",
    "bicycling": "/v5/direction/bicycling",
    "transit": "/v3/direction/transit/integrated",
}

# infocode 分类（高德“错误码说明”）。额度：日 / 月配额、海外配额、套餐用完或到期
QUOTA_CODES = frozenset({"10003", "10010", "10029", "10044", "10045", "40000", "40002", "40003"})
RATE_CODES = frozenset({"10004", "10014", "10019", "10020", "10021"})
# key 无效、没开这项服务、IP / 域名 / 签名不对、权限不足、key 被回收、没有可用接口
AUTH_CODES = frozenset(
    {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10011", "10012", "10013", "10041"}
)
UNAVAILABLE_CODES = frozenset({"10015", "10016", "10017", "20003"})


def next_reset(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(BEIJING)
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (month_start + timedelta(days=32)).replace(day=1)


def classify(infocode: str) -> str:
    if infocode in QUOTA_CODES:
        return "quota"
    if infocode in RATE_CODES:
        return "rate"
    if infocode in AUTH_CODES:
        return "auth"
    if infocode in UNAVAILABLE_CODES:
        return "unavailable"
    # 2xxxx：参数不对、算不出路线、超出范围；3xxxx：引擎没给出结果（地址解析查不到时回 30001）
    if infocode[:1] in {"2", "3"}:
        return "rejected"
    return "unavailable"


def parse_place(raw: Any) -> Place | None:
    if not isinstance(raw, dict):
        return None
    poi_id = text(raw.get("id"), 64)
    name = text(raw.get("name"), 120)
    if not poi_id or not name:
        return None
    business = raw.get("business")
    if not isinstance(business, dict):
        business = {}
    photo = None
    photos = raw.get("photos")
    if isinstance(photos, list):
        photo = next(
            (
                url
                for item in photos[:5]
                if isinstance(item, dict) and (url := official_media_url(item.get("url"), MEDIA_DOMAINS))
            ),
            None,
        )
    return Place(
        provider=AMAP,
        id=poi_id,
        name=name,
        location=Point.from_amap(raw.get("location")),
        address=text(raw.get("address"), 240),
        district=text(raw.get("adname"), 40),
        city=text(raw.get("cityname"), 40) or text(raw.get("pname"), 40),
        category=text(raw.get("type"), 160),
        distance_m=non_negative_int(raw.get("distance")),
        rating=rating(business.get("rating")),
        reference_cost_yuan=amount(business.get("cost")),
        open_hours=text(business.get("opentime_week"), 240) or text(business.get("opentime_today"), 240),
        business_area=text(business.get("business_area"), 120),
        photo_url=photo,
    )


def parse_places(body: dict) -> list[Place]:
    pois = body.get("pois")
    if not isinstance(pois, list):
        return []
    places: list[Place] = []
    seen: set[str] = set()
    for raw in pois[:50]:
        place = parse_place(raw)
        if place is None or place.id in seen:
            continue
        seen.add(place.id)
        places.append(place)
    return places


def parse_geocodes(body: dict) -> list[GeoCandidate]:
    """候选只合并坐标完全相同的（高德对道路常重复返回）。
    以前按 1 公里内合并，把外滩定到了浦东的同名小区（Fusion c3bc81cb）。"""
    raw = body.get("geocodes")
    if not isinstance(raw, list):
        return []
    candidates: list[GeoCandidate] = []
    seen: set[Point] = set()
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        location = Point.from_amap(item.get("location"))
        if location is None or location in seen:
            continue
        seen.add(location)
        candidates.append(
            GeoCandidate(
                location=location,
                city=text(item.get("city"), 40) or text(item.get("province"), 40),
                district=text(item.get("district"), 40),
                level=text(item.get("level"), 20),
            )
        )
    return candidates


def parse_reverse_city(body: dict) -> str | None:
    regeocode = body.get("regeocode")
    component = regeocode.get("addressComponent") if isinstance(regeocode, dict) else None
    if not isinstance(component, dict):
        return None
    # 直辖市的 city 是 []，用省
    return text(component.get("city"), 40) or text(component.get("province"), 40)


def _node_name(value: Any, max_chars: int) -> str | None:
    return text(value.get("name"), max_chars) if isinstance(value, dict) else None


def _metrics(raw: dict) -> tuple[int | None, int | None]:
    return non_negative_int(raw.get("distance")), non_negative_int(raw.get("duration"))


def parse_transit_legs(segments: Any) -> list[Leg]:
    """每段依次是步行、公交 / 地铁、铁路；空的段高德给 []（Fusion APT:1933-1989）。"""
    if not isinstance(segments, list):
        return []
    legs: list[Leg] = []
    for segment in segments[:MAX_LEGS]:
        if not isinstance(segment, dict):
            continue
        walking = segment.get("walking")
        if isinstance(walking, dict):
            distance, duration = _metrics(walking)
            legs.append(Leg(kind="walking", distance_m=distance, duration_s=duration))
        bus = segment.get("bus")
        lines = bus.get("buslines") if isinstance(bus, dict) else None
        line = next((item for item in (lines or [])[:1] if isinstance(item, dict)), None)
        if line is not None:
            name = text(line.get("name"), 120)
            line_type = text(line.get("type"), 120)
            distance, duration = _metrics(line)
            legs.append(
                Leg(
                    kind="subway" if is_subway(name, line_type) else "bus" if name or line_type else "other",
                    line_name=name,
                    departure_stop=_node_name(line.get("departure_stop"), 120),
                    arrival_stop=_node_name(line.get("arrival_stop"), 120),
                    via_stop_count=non_negative_int(line.get("via_num")),
                    distance_m=distance,
                    duration_s=duration,
                    entrance=_node_name(segment.get("entrance"), 80),
                    exit=_node_name(segment.get("exit"), 80),
                )
            )
        railway = segment.get("railway")
        if isinstance(railway, dict):
            distance, duration = _metrics(railway)
            leg = Leg(
                kind="other",
                line_name=text(railway.get("name"), 120) or text(railway.get("trip"), 120),
                departure_stop=_node_name(railway.get("departure_stop"), 120),
                arrival_stop=_node_name(railway.get("arrival_stop"), 120),
                distance_m=distance,
                duration_s=duration,
            )
            # 没有铁路时高德给的是只有空列表的对象
            if any(v is not None for v in (leg.line_name, leg.departure_stop, leg.arrival_stop, distance, duration)):
                legs.append(leg)
    return legs[:MAX_LEGS]


def _transit_plan(raw: Any) -> Route | None:
    if not isinstance(raw, dict):
        return None
    duration = non_negative_int(raw.get("duration"))
    if duration is None:
        return None
    # 不给 distance：公交方案的总距离不可靠（Fusion TOML:282 写着 route.distance 是两头的步行距离）
    return finish_transit(
        Route(
            mode="transit",
            duration_s=duration,
            walking_distance_m=non_negative_int(raw.get("walking_distance")),
            fare_yuan=amount(raw.get("cost")),
            legs=parse_transit_legs(raw.get("segments")),
        )
    )


def parse_route(body: dict, mode: str) -> Route | None:
    route = body.get("route")
    if not isinstance(route, dict):
        return None
    if mode == "transit":
        plans = [plan for raw in (route.get("transits") or [])[:3] if (plan := _transit_plan(raw)) is not None]
        if not plans:
            return None
        primary = plans[0]
        primary.alternatives = plans[1 : 1 + MAX_ALTERNATIVES]
        return primary
    paths = route.get("paths")
    path = paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], dict) else None
    if path is None:
        return None
    distance = non_negative_int(path.get("distance"))
    if distance is None:
        distance = non_negative_int(route.get("distance"))
    duration = non_negative_int(path.get("duration"))
    if duration is None and isinstance(path.get("cost"), dict):
        duration = non_negative_int(path["cost"].get("duration"))
    if distance is None and duration is None:
        return None
    return Route(
        mode=mode,
        distance_m=distance,
        duration_s=duration,
        summary=text(path.get("strategy"), 160),
        toll_yuan=amount(path.get("tolls")) if mode == "driving" else None,
    )


class AmapClient:
    provider = AMAP

    def __init__(self, key: str, http: httpx.AsyncClient, gate: Gate):
        self._key = key
        self._http = http
        self._gate = gate

    async def _get(self, api: str, group: str, path: str, params: dict[str, Any]) -> dict:
        async def send() -> dict:
            try:
                response = await self._http.get(f"{BASE_URL}{path}", params={**params, "key": self._key})
            except httpx.TimeoutException:
                raise UpstreamError("timeout", AMAP, api) from None
            except httpx.HTTPError:
                raise UpstreamError("unavailable", AMAP, api) from None
            if response.status_code == 429:
                raise UpstreamError("rate", AMAP, api, "http_429")
            if response.status_code != 200:
                raise UpstreamError("unavailable", AMAP, api, f"http_{response.status_code}")
            try:
                body = response.json()
            except ValueError:
                raise UpstreamError("invalid_response", AMAP, api) from None
            if not isinstance(body, dict):
                raise UpstreamError("invalid_response", AMAP, api)
            if str(body.get("status")) == "1":
                return body
            code = str(body.get("infocode") or "")
            kind = classify(code)
            raise UpstreamError(kind, AMAP, api, code, reset_at=next_reset() if kind == "quota" else None)

        return await self._gate.call(group, api, send)

    async def text_search(self, terms: list[str], city: str | None) -> list[Place]:
        params: dict[str, Any] = {"keywords": "|".join(terms), "show_fields": SEARCH_FIELDS, "page_size": PAGE_SIZE}
        if city:
            params.update(region=city, city_limit="true")
        return parse_places(await self._get("place_text", "search", "/v5/place/text", params))

    async def around_search(self, terms: list[str], center: Point, radius_m: int) -> list[Place]:
        params = {
            "keywords": "|".join(terms),
            "location": center.amap(),
            "radius": radius_m,
            "show_fields": SEARCH_FIELDS,
            "page_size": PAGE_SIZE,
        }
        return parse_places(await self._get("place_around", "search", "/v5/place/around", params))

    async def detail(self, poi_id: str) -> Place | None:
        body = await self._get(
            "place_detail", "search", "/v5/place/detail", {"id": poi_id, "show_fields": SEARCH_FIELDS}
        )
        return next((place for place in parse_places(body) if place.id == poi_id), None)

    async def geocode(self, label: str, city: str | None) -> GeoLookup:
        params = {"address": label, **({"city": city} if city else {})}
        return GeoLookup(parse_geocodes(await self._get("geocode", "geocode", "/v3/geocode/geo", params)))

    async def reverse_city(self, point: Point) -> str | None:
        return parse_reverse_city(
            await self._get("regeocode", "regeocode", "/v3/geocode/regeo", {"location": point.amap()})
        )

    async def route(self, mode: str, origin: Endpoint, destination: Endpoint) -> Route | None:
        params: dict[str, Any] = {"origin": origin.location.amap(), "destination": destination.location.amap()}
        if mode == "transit":
            # 高德算公交必须有两端城市；缺了交给腾讯（腾讯不要城市）
            if not origin.city or not destination.city:
                raise UpstreamError("unsupported", AMAP, "route_transit", "no_city")
            params.update(city=origin.city, cityd=destination.city)
        elif mode == "driving":
            params["extensions"] = "base"
        elif mode == "bicycling":
            params["show_fields"] = "cost"
        body = await self._get(f"route_{mode}", f"route_{mode}", ROUTE_PATHS[mode], params)
        return parse_route(body, mode)

    async def convert_gps(self, lng: float, lat: float) -> Point:
        body = await self._get(
            "convert",
            "convert",
            "/v3/assistant/coordinate/convert",
            {"locations": f"{lng:.6f},{lat:.6f}", "coordsys": "gps"},
        )
        locations = body.get("locations")
        point = Point.from_amap(locations.split(";", 1)[0]) if isinstance(locations, str) else None
        if point is None:
            raise UpstreamError("invalid_response", AMAP, "convert")
        return point
