"""地名怎么定到一个点：两家用同一套规则，照 Fusion（amap_product_tools.py 的 _resolve_geo、_select_* 几个函数）。

1. 先看地名解析的候选（高德地址解析；腾讯输入提示里名字就是这个地名的，见 tmap.name_candidates）：
   只有一个，就用它（给了城市却对不上，当没解析出来）；有好几个，先按城市筛（没给城市时用起点的城市，Fusion APT:738）。
   筛完还剩好几个，不替用户挑，回重名，带最多 5 个编号候选；模型带上编号再查就用那个（Fusion 84f79858、30efef77）。
2. 解析不出来，改用关键词搜索兜底（Fusion 77a56962）：
   - 有城市：名字里含这个地名、城市对得上的，取多出来的字最少的那个，并列就不算；
     还定不下，取搜索结果里第一个名字含这个地名的，城市要对得上。
   - 没城市：只认名字和地名完全一样、地名里写了城市名、而且只有一个的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.model import Endpoint, GeoCandidate, Place, city_matches, match_text, mentions_city

MAX_CANDIDATES = 5


@dataclass
class Resolution:
    endpoint: Endpoint | None = None
    candidates: list[GeoCandidate] = field(default_factory=list)


def resolve_candidates(
    candidates: list[GeoCandidate],
    *,
    label: str,
    requested_city: str | None,
    preferred_city: str | None = None,
    choice: int | None = None,
) -> Resolution:
    if len(candidates) == 1:
        only = candidates[0]
        if requested_city and not city_matches(requested_city, only.city):
            return Resolution()
        return Resolution(Endpoint(label, only.location, only.city))
    selection_city = requested_city or preferred_city
    if selection_city:
        candidates = [c for c in candidates if city_matches(selection_city, c.city)]
    if len(candidates) == 1:
        return Resolution(Endpoint(label, candidates[0].location, candidates[0].city))
    if not candidates:
        return Resolution()
    candidates = candidates[:MAX_CANDIDATES]
    if choice is not None and choice <= len(candidates):
        chosen = candidates[choice - 1]
        return Resolution(Endpoint(label, chosen.location, chosen.city))
    return Resolution(candidates=candidates)


def select_in_city(places: list[Place], *, label: str, city: str) -> Endpoint | None:
    wanted = match_text(label)
    if not wanted:
        return None
    scored: list[tuple[int, Place]] = []
    for place in places:
        name = match_text(place.name)
        if place.location is None or wanted not in name or not city_matches(city, place.city):
            continue
        scored.append((len(name) - len(wanted), place))
    if scored:
        best = min(score for score, _ in scored)
        top = [place for score, place in scored if score == best]
        if len(top) == 1:
            return Endpoint(label, top[0].location, top[0].city)  # type: ignore[arg-type]
    # 并列时照 Fusion：取搜索结果里第一个名字含这个地名的（Fusion 再查一次详情确认城市，这里搜索结果里就有）
    first = next((place for place in places if wanted in match_text(place.name)), None)
    if first is not None and first.location is not None and city_matches(city, first.city):
        return Endpoint(label, first.location, first.city)
    return None


def select_anywhere(places: list[Place], *, label: str) -> Endpoint | None:
    wanted = match_text(label)
    if not wanted:
        return None
    exact = [
        place
        for place in places
        if place.location is not None and match_text(place.name) == wanted and mentions_city(wanted, place.city)
    ]
    if len(exact) != 1:
        return None
    return Endpoint(label, exact[0].location, exact[0].city)  # type: ignore[arg-type]
