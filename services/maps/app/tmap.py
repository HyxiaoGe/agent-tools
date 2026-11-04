"""腾讯位置服务 Web 服务（apis.map.qq.com）。
只请求这里列出的固定路径，不跟随重定向、不重试；key 只拼在请求参数里，不进日志。

走 REST 不走腾讯 MCP：MCP 返回给人看的中文段落，同一字段各工具写法不一样；REST 是 JSON，地址解析也能用（11-04 样本）。
腾讯的数据比高德少：没有评分、参考消费、营业时间和图片。用时以分钟计、票价以分计，这里换成秒和元。
额度按接口分别计（个人额度：地点搜索每天 200 次，其他接口每天 6000 次，账号下各 key 共享），北京时间次日零点恢复；
REST 和 MCP 的额度在控制台里分开分配。

地名解析用输入提示（一次 10 个，带省市区），不用地址解析：地址解析一次只回一个结果，看不出重名。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.model import (
    MAX_ALTERNATIVES,
    MAX_LEGS,
    TMAP,
    Endpoint,
    GeoCandidate,
    GeoLookup,
    Leg,
    Place,
    Point,
    Route,
    amount,
    city_text,
    finish_transit,
    is_subway,
    match_text,
    non_negative_int,
    text,
)
from app.upstream import BEIJING, Gate, UpstreamError

BASE_URL = "https://apis.map.qq.com"
PAGE_SIZE = 10
MINUTE = 60
ROUTE_MODES = ("driving", "walking", "bicycling", "transit")

QUOTA_STATUSES = frozenset({121})
RATE_STATUSES = frozenset({120})
# 来源 / 签名 / IP 未授权、功能未授权、key 无效、没开 WebService、key 格式错误
AUTH_STATUSES = frozenset({110, 111, 112, 113, 190, 199, 311})


def next_reset(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(BEIJING)
    return current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def classify(status: int) -> str:
    if status in QUOTA_STATUSES:
        return "quota"
    if status in RATE_STATUSES:
        return "rate"
    if status in AUTH_STATUSES:
        return "auth"
    # 3xx：参数不对、查无结果、起终点距离超长（373）等
    if 300 <= status < 400:
        return "rejected"
    return "unavailable"


def parse_place(raw: Any) -> Place | None:
    if not isinstance(raw, dict):
        return None
    poi_id = text(raw.get("id"), 64)
    name = text(raw.get("title"), 120)
    if not poi_id or not name:
        return None
    # 地点搜索、详情把省市区放在 ad_info 里，输入提示放在顶层
    area = raw.get("ad_info")
    if not isinstance(area, dict):
        area = raw
    return Place(
        provider=TMAP,
        id=poi_id,
        name=name,
        location=Point.from_tmap(raw.get("location")),
        address=text(raw.get("address"), 240),
        district=text(area.get("district"), 40),
        city=text(area.get("city"), 40) or text(area.get("province"), 40),
        category=text(raw.get("category"), 160),
        distance_m=non_negative_int(raw.get("_distance")),
    )


def parse_places(body: dict) -> list[Place]:
    data = body.get("data")
    if not isinstance(data, list):
        return []
    places: list[Place] = []
    seen: set[str] = set()
    for raw in data[:50]:
        place = parse_place(raw)
        if place is None or place.id in seen:
            continue
        seen.add(place.id)
        places.append(place)
    return places


def name_candidates(places: list[Place], label: str) -> list[GeoCandidate]:
    """输入提示里哪些算“这个地名”：名称和地名一样，或者去掉开头的城市名、区县名后一样（“宁波鼓楼”“磁县鼓楼”算“鼓楼”）。
    别的只是名字里带这个词（“外滩观景台”），不算候选，留给按名称挑的那一步。坐标完全相同的只留一个。"""
    wanted = match_text(label)
    if not wanted:
        return []
    candidates: list[GeoCandidate] = []
    seen: set[Point] = set()
    for place in places:
        if place.location is None or place.location in seen:
            continue
        name = match_text(place.name)
        forms = {name}
        for area in (place.city, place.district):
            prefix = city_text(area) if area else ""
            if len(prefix) >= 2 and name.startswith(prefix):
                forms.add(name[len(prefix) :])
        if wanted not in forms:
            continue
        seen.add(place.location)
        candidates.append(
            GeoCandidate(location=place.location, city=place.city, district=place.district, category=place.category)
        )
    return candidates


def parse_reverse_city(body: dict) -> str | None:
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    for key in ("address_component", "ad_info"):
        part = result.get(key)
        if isinstance(part, dict) and (city := text(part.get("city"), 40) or text(part.get("province"), 40)):
            return city
    return None


def _minutes(value: Any) -> int | None:
    minutes = non_negative_int(value)
    return minutes * MINUTE if minutes is not None else None


def _node(value: Any, key: str = "title") -> str | None:
    return text(value.get(key), 120) if isinstance(value, dict) else None


def _transit_plan(raw: Any) -> Route | None:
    if not isinstance(raw, dict):
        return None
    duration = _minutes(raw.get("duration"))
    if duration is None:
        return None
    legs: list[Leg] = []
    walking = 0
    steps = raw.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if step.get("mode") == "WALKING":
            distance = non_negative_int(step.get("distance"))
            walking += distance or 0
            legs.append(Leg(kind="walking", distance_m=distance, duration_s=_minutes(step.get("duration"))))
        elif step.get("mode") == "TRANSIT":
            lines = step.get("lines")
            line = next((item for item in (lines or [])[:1] if isinstance(item, dict)), None)
            if line is None:
                continue
            name = text(line.get("title"), 120)
            vehicle = line.get("vehicle")
            stations = non_negative_int(line.get("station_count"))
            geton, getoff = line.get("geton"), line.get("getoff")
            legs.append(
                Leg(
                    kind="subway"
                    if vehicle == "SUBWAY" or is_subway(name, None)
                    else "bus"
                    if vehicle == "BUS"
                    else "other",
                    line_name=name,
                    departure_stop=_node(geton),
                    arrival_stop=_node(getoff),
                    # station_count 数的是坐了几站，比高德的途经站数多 1（同一趟 2 号线：腾讯 11、高德 10）
                    via_stop_count=stations - 1 if stations else None,
                    distance_m=non_negative_int(line.get("distance")),
                    duration_s=_minutes(line.get("duration")),
                    entrance=_node(geton.get("exit")) if isinstance(geton, dict) else None,
                    exit=_node(getoff.get("exit")) if isinstance(getoff, dict) else None,
                )
            )
        if len(legs) >= MAX_LEGS:
            break
    price = amount(raw.get("price"))
    return finish_transit(
        Route(
            mode="transit",
            duration_s=duration,
            walking_distance_m=walking,
            fare_yuan=amount(price / 100) if price is not None else None,
            legs=legs,
        )
    )


def parse_route(body: dict, mode: str) -> Route | None:
    result = body.get("result")
    routes = result.get("routes") if isinstance(result, dict) else None
    if not isinstance(routes, list) or not routes:
        return None
    if mode == "transit":
        plans = [plan for raw in routes[:3] if (plan := _transit_plan(raw)) is not None]
        if not plans:
            return None
        primary = plans[0]
        primary.alternatives = plans[1 : 1 + MAX_ALTERNATIVES]
        return primary
    raw = routes[0]
    if not isinstance(raw, dict):
        return None
    distance = non_negative_int(raw.get("distance"))
    duration = _minutes(raw.get("duration"))
    if distance is None and duration is None:
        return None
    return Route(
        mode=mode,
        distance_m=distance,
        duration_s=duration,
        toll_yuan=amount(raw.get("toll")) if mode == "driving" else None,
    )


def _region(city: str) -> str:
    # 城市名放进 region(…) 语法里，去掉会改变语法的字符
    return "".join(ch for ch in city if ch not in "(),;")


class TmapClient:
    provider = TMAP

    def __init__(self, key: str, http: httpx.AsyncClient, gate: Gate):
        self._key = key
        self._http = http
        self._gate = gate

    async def _get(self, api: str, group: str, path: str, params: dict[str, Any]) -> dict:
        async def send() -> dict:
            try:
                response = await self._http.get(f"{BASE_URL}{path}", params={**params, "key": self._key})
            except httpx.TimeoutException:
                raise UpstreamError("timeout", TMAP, api) from None
            except httpx.HTTPError:
                raise UpstreamError("unavailable", TMAP, api) from None
            if response.status_code == 429:
                raise UpstreamError("rate", TMAP, api, "http_429")
            if response.status_code != 200:
                raise UpstreamError("unavailable", TMAP, api, f"http_{response.status_code}")
            try:
                body = response.json()
            except ValueError:
                raise UpstreamError("invalid_response", TMAP, api) from None
            if not isinstance(body, dict):
                raise UpstreamError("invalid_response", TMAP, api)
            status = body.get("status")
            if status == 0:
                return body
            if isinstance(status, bool) or not isinstance(status, int):
                raise UpstreamError("invalid_response", TMAP, api)
            kind = classify(status)
            raise UpstreamError(kind, TMAP, api, str(status), reset_at=next_reset() if kind == "quota" else None)

        return await self._gate.call(group, api, send)

    async def _suggest(self, keyword: str, city: str | None) -> list[Place]:
        params: dict[str, Any] = {"keyword": keyword, "page_size": PAGE_SIZE}
        if city:
            params.update(region=city, region_fix=1)
        return parse_places(await self._get("suggestion", "suggestion", "/ws/place/v1/suggestion/", params))

    async def text_search(self, terms: list[str], city: str | None) -> list[Place]:
        # 腾讯没有“或”的写法，只搜第一个词（结果里会注明）
        if not city:
            return await self._suggest(terms[0], None)
        params = {"keyword": terms[0], "boundary": f"region({_region(city)},0)", "page_size": PAGE_SIZE}
        return parse_places(await self._get("place_search", "place_search", "/ws/place/v1/search", params))

    async def around_search(self, terms: list[str], center: Point, radius_m: int) -> list[Place]:
        params = {
            "keyword": terms[0],
            "boundary": f"nearby({center.tmap()},{radius_m},0)",
            "orderby": "_distance",
            "page_size": PAGE_SIZE,
        }
        return parse_places(await self._get("place_around", "place_search", "/ws/place/v1/search", params))

    async def detail(self, poi_id: str) -> Place | None:
        body = await self._get("place_detail", "place_detail", "/ws/place/v1/detail", {"id": poi_id})
        return next((place for place in parse_places(body) if place.id == poi_id), None)

    async def geocode(self, label: str, city: str | None) -> GeoLookup:
        places = await self._suggest(label, city)
        return GeoLookup(name_candidates(places, label), places)

    async def reverse_city(self, point: Point) -> str | None:
        return parse_reverse_city(
            await self._get("regeocode", "geocoder", "/ws/geocoder/v1/", {"location": point.tmap()})
        )

    async def route(self, mode: str, origin: Endpoint, destination: Endpoint) -> Route | None:
        params = {"from": origin.location.tmap(), "to": destination.location.tmap()}
        body = await self._get(f"route_{mode}", f"route_{mode}", f"/ws/direction/v1/{mode}/", params)
        return parse_route(body, mode)

    async def convert_gps(self, lng: float, lat: float) -> Point:
        body = await self._get(
            "convert", "translate", "/ws/coord/v1/translate", {"locations": f"{lat:.6f},{lng:.6f}", "type": 1}
        )
        locations = body.get("locations")
        point = Point.from_tmap(locations[0]) if isinstance(locations, list) and locations else None
        if point is None:
            raise UpstreamError("invalid_response", TMAP, "convert")
        return point
