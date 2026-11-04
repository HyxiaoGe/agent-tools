"""两家共用的数据结构，和从上游 JSON 取值、比较地名的小函数。

取值照 Fusion（app/services/mcp/amap_product_tools.py）：只按明确的字段取，类型不对就当没有（高德的空值是 []），
数字只收非负的，文字里像密钥的整条不要。地名比较也照 Fusion：只留数字、字母、汉字再比，城市去掉“市”“地区”等后缀。
坐标只在服务里用（算周边、算路线、给模型判断重名），不放进地点和路线结果（Fusion 0909d20f）。
两家都是 GCJ-02，同一次调用里可以混用；高德写“经度,纬度”，腾讯写“纬度,经度”，只在各自的客户端里换顺序。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

AMAP = "amap"
TMAP = "tmap"

COORDINATES = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*,\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*$")
_SECRET_VALUE = r'"(?:\\.|[^"\\])+"|\'(?:\\.|[^\'\\])+\'|[a-z0-9._~+/=-]{4,}'
# 参数里有这种写法就不发给上游；上游文字里有就整条不要（Fusion APT:105-112）
SECRET = re.compile(
    rf"\b(?:api[ _-]*key|client[ _-]*secret|password|access[ _-]*token|token|cookie|session[ _-]*id)\s*[:=]\s*"
    rf"(?:{_SECRET_VALUE})"
    rf"|\b(?:proxy[ _-]*authorization|authorization)\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?(?:{_SECRET_VALUE})",
    re.IGNORECASE,
)
_CITY_SUFFIXES = ("自治州", "地区", "市", "盟")


@dataclass(frozen=True)
class Point:
    """GCJ-02 坐标。"""

    lng: float
    lat: float

    @classmethod
    def of(cls, lng: Any, lat: Any) -> Point | None:
        lng_value, lat_value = number(lng), number(lat)
        if lng_value is None or lat_value is None:
            return None
        if not (-180 <= lng_value <= 180 and -90 <= lat_value <= 90) or (lng_value == 0 and lat_value == 0):
            return None  # 0,0 是上游没解析出来时给的（腾讯 MCP 的 geocoder 就这样）
        return cls(lng_value, lat_value)

    @classmethod
    def from_amap(cls, value: Any) -> Point | None:
        if not isinstance(value, str) or not COORDINATES.fullmatch(value):
            return None
        lng, lat = value.split(",", 1)
        return cls.of(lng, lat)

    @classmethod
    def from_tmap(cls, value: Any) -> Point | None:
        if not isinstance(value, dict):
            return None
        return cls.of(value.get("lng"), value.get("lat"))

    def amap(self) -> str:
        return f"{self.lng:.6f},{self.lat:.6f}"

    def tmap(self) -> str:
        return f"{self.lat:.6f},{self.lng:.6f}"

    def text(self) -> str:
        """给模型判断重名用的写法（经度,纬度）。"""
        return self.amap()


@dataclass
class Place:
    provider: str
    id: str
    name: str
    location: Point | None = None
    address: str | None = None
    district: str | None = None
    city: str | None = None
    category: str | None = None
    distance_m: int | None = None
    rating: float | int | None = None
    reference_cost_yuan: float | int | None = None
    open_hours: str | None = None
    business_area: str | None = None
    photo_url: str | None = None

    @property
    def place_id(self) -> str:
        return f"{self.provider}_{self.id}"


@dataclass(frozen=True)
class GeoCandidate:
    """地名解析出的一个候选。level 是高德的匹配级别（兴趣点、区县……），category 是腾讯的分类。"""

    location: Point
    city: str | None = None
    district: str | None = None
    level: str | None = None
    category: str | None = None


@dataclass
class GeoLookup:
    """一次地名解析的结果。places 只有腾讯有：它用输入提示解析地名，同一批结果接着用来按名称挑，不再另查一次。"""

    candidates: list[GeoCandidate]
    places: list[Place] | None = None


@dataclass(frozen=True)
class Endpoint:
    """解析好的地点：label 是调用方给的地名（当前位置时为空），city 可能没有。"""

    label: str | None
    location: Point
    city: str | None = None
    source: str = "named"


@dataclass
class Leg:
    kind: str | None = None  # walking / subway / bus / other
    line_name: str | None = None
    departure_stop: str | None = None
    arrival_stop: str | None = None
    via_stop_count: int | None = None
    distance_m: int | None = None
    duration_s: int | None = None
    entrance: str | None = None
    exit: str | None = None


@dataclass
class Route:
    mode: str
    distance_m: int | None = None
    duration_s: int | None = None
    summary: str | None = None
    toll_yuan: float | int | None = None
    fare_yuan: float | int | None = None
    transfers: int | None = None
    transit_type: str | None = None
    walking_distance_m: int | None = None
    legs: list[Leg] = field(default_factory=list)
    alternatives: list[Route] = field(default_factory=list)


MAX_LEGS = 8
MAX_ALTERNATIVES = 2
_SUBWAY = re.compile(r"地铁|轨道交通|轻轨|磁悬浮|\bsubway\b|\bmetro\b", re.IGNORECASE)


def is_subway(line_name: str | None, line_type: str | None) -> bool:
    return bool(_SUBWAY.search(f"{line_name or ''} {line_type or ''}"))


def finish_transit(route: Route) -> Route:
    """公交方案的分类和换乘次数：只看坐车的几段（Fusion APT:1893-1918、2014-2022）。"""
    route.legs = route.legs[:MAX_LEGS]
    rides = [leg.kind for leg in route.legs if leg.kind in {"subway", "bus", "other"}]
    kinds = set(rides)
    if kinds == {"subway"}:
        route.transit_type = "subway"
    elif kinds == {"bus"}:
        route.transit_type = "bus"
    elif kinds == {"subway", "bus"}:
        route.transit_type = "mixed"
    else:
        route.transit_type = "public_transit"
    route.transfers = max(0, len(rides) - 1) if rides else None
    return route


def text(value: Any, max_chars: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or SECRET.search(value):
        return None
    return value[:max_chars]


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "" or isinstance(value, list | dict):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def non_negative_int(value: Any) -> int | None:
    parsed = number(value)
    if parsed is None or parsed < 0:
        return None
    return int(parsed)


def amount(value: Any) -> float | int | None:
    """非负金额、评分：整数照整数给，其余留两位小数。"""
    parsed = number(value)
    if parsed is None or parsed < 0:
        return None
    return int(parsed) if parsed.is_integer() else round(parsed, 2)


def rating(value: Any) -> float | int | None:
    parsed = amount(value)
    return parsed if parsed is not None and parsed <= 5 else None


def official_media_url(value: Any, domains: tuple[str, ...]) -> str | None:
    """只收服务商自己域名下的 https 图片（Fusion APT:1718-1731）。"""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in {None, 443} or parts.fragment:
        return None
    if host in domains or host.endswith(tuple(f".{d}" for d in domains)):
        return value
    return None


def match_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def city_text(value: str) -> str:
    normalized = match_text(value)
    for suffix in _CITY_SUFFIXES:
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def city_matches(requested: str | None, candidate: str | None) -> bool:
    if not requested or not isinstance(candidate, str):
        return False
    wanted = city_text(requested)
    return bool(wanted) and wanted == city_text(candidate)


def mentions_city(normalized_label: str, city: str | None) -> bool:
    if not city:
        return False
    normalized = city_text(city)
    return len(normalized) >= 2 and normalized in normalized_label


def keyword_terms(query: str) -> list[str]:
    """用户给的几个备选词：| 或中英文逗号、顿号分开；空格算词的一部分（Fusion APT:1168-1173）。"""
    terms = [term.strip() for term in re.split(r"[|,，、]", query) if term.strip()]
    return list(dict.fromkeys(terms)) or [query.strip()]
