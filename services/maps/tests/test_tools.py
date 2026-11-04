"""两个工具的整套流程：先高德后腾讯怎么转、地名怎么定、当前位置、编号、超时，以及各种查不到。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from app.main import build_service
from app.tools import Device, MapsService, ToolFailure, parse_place_args, parse_route_args
from app.upstream import BEIJING, QuotaMemo, Usage
from tests.fake import AMAP_EMPTY, AMAP_QUOTA, TMAP_QUOTA, Delay, FakeUpstream, settings

pytestmark = pytest.mark.anyio

DEVICE = Device(longitude=121.4855, latitude=31.242)
GEO = "/v3/geocode/geo"
TEXT = "/v5/place/text"
AROUND = "/v5/place/around"
SUGGEST = "/ws/place/v1/suggestion/"
SEARCH = "/ws/place/v1/search"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def amap_geo(*candidates: tuple[str, str, str, str]) -> dict[str, Any]:
    """(坐标, 城市, 区, 级别)"""
    return {
        "status": "1",
        "count": str(len(candidates)),
        "geocodes": [
            {"location": loc, "city": city, "district": district, "level": level}
            for loc, city, district, level in candidates
        ],
    }


HONGQIAO_GEO = amap_geo(("121.320,31.194", "上海市", "闵行区", "火车站"))


def service(fake: FakeUpstream, **overrides: Any) -> MapsService:
    return build_service(settings(**overrides), fake.client(), Usage(), QuotaMemo())


def place(**kwargs: Any):
    values: dict[str, Any] = {"anchor_source": "none"}
    values.update(kwargs)
    return parse_place_args(**values)


def route(**kwargs: Any):
    values: dict[str, Any] = {"origin_source": "named", "destination_source": "named", "modes": ["driving"]}
    values.update(kwargs)
    return parse_route_args(**values)


def today_usage(svc: MapsService) -> dict[str, Any]:
    summary = svc.usage.summary(datetime.now(BEIJING).date())
    return {"fallbacks": summary["fallbacks"], **{p["provider"]: p for p in summary["providers"]}}


async def failure(coro) -> ToolFailure:
    with pytest.raises(ToolFailure) as caught:
        await coro
    return caught.value


class TestPlaceSearch:
    async def test_text_search_in_city_uses_amap(self):
        fake = FakeUpstream().sample("amap", "place_text")
        result = await service(fake).place_search(place(query="火锅", city="上海", limit=3), app="lingdraft")
        assert result["status"] == "success" and result["result_count"] == 3 and result["providers"] == ["amap"]
        first = result["places"][0]
        assert first["place_id"] == "amap_B0FFKKRXAZ" and first["provider"] == "amap"
        assert first["map_url"] == "https://uri.amap.com/marker?poiid=B0FFKKRXAZ&src=lingdraft&callnative=0"
        assert first["reference_cost_yuan"] == 120 and "location" not in first
        assert result["limitations"] == []
        assert "anchor" not in result and fake.paths("tmap") == []

    async def test_quota_moves_to_tmap_and_is_remembered(self):
        fake = FakeUpstream().on("amap", TEXT, AMAP_QUOTA).sample("tmap", "search_region")
        svc = service(fake)
        for _ in range(2):
            result = await svc.place_search(place(query="火锅|烤肉", city="上海"))
            assert result["providers"] == ["tmap"] and result["places"][0]["place_id"].startswith("tmap_")
            assert "map_url" not in result["places"][0]
            assert result["limitations"] == ["first_term_only"]
        assert fake.paths("amap") == [TEXT]  # 第二次记着高德用尽，不再请求
        usage = today_usage(svc)
        assert (usage["amap"]["requests"], usage["amap"]["quota"], usage["amap"]["skipped"]) == (1, 1, 1)
        assert (usage["tmap"]["requests"], usage["fallbacks"]) == (2, 2)

    @pytest.mark.parametrize(
        "response", [500, {"status": "0", "infocode": "10001"}, {"status": "0", "infocode": "10021"}]
    )
    async def test_errors_and_bad_keys_move_to_tmap(self, response):
        fake = FakeUpstream().on("amap", AROUND, response).sample("tmap", "search_nearby_3000")
        fake.on("amap", "/v3/assistant/coordinate/convert", response).sample("tmap", "translate")
        result = await service(fake).place_search(place(query="咖啡", anchor_source="current_location"), DEVICE)
        assert result["providers"] == ["tmap"] and result["places"][0]["distance_m"] == 53
        assert result["anchor"] == {"source": "current_location"}

    async def test_rejected_requests_do_not_move(self):
        fake = FakeUpstream().on("amap", TEXT, {"status": "0", "infocode": "20000"})
        assert (await failure(service(fake).place_search(place(query="火锅")))).code == "unavailable"
        assert fake.paths("tmap") == []

    async def test_zero_results_do_not_move(self):
        fake = FakeUpstream().on("amap", TEXT, AMAP_EMPTY)
        result = await service(fake).place_search(place(query="不存在的店", city="上海"))
        assert result["result_count"] == 0 and result["places"] == [] and fake.paths("tmap") == []

    async def test_both_exhausted_reports_earliest_reset(self):
        fake = FakeUpstream().on("amap", TEXT, AMAP_QUOTA).on("tmap", SEARCH, TMAP_QUOTA)
        error = await failure(service(fake).place_search(place(query="火锅", city="上海")))
        assert error.code == "quota_exhausted" and error.reset_at is not None
        assert error.reset_at.astimezone(BEIJING).hour == 0

    async def test_no_keys(self):
        fake = FakeUpstream()
        svc = service(fake, amap_api_key="", tmap_api_key="")
        assert svc.providers == []
        assert (await failure(svc.place_search(place(query="火锅")))).code == "unavailable"

    async def test_named_anchor_ambiguity_and_choice(self):
        fake = FakeUpstream().sample("amap", "geo_bund").sample("amap", "place_around")
        svc = service(fake)
        error = await failure(svc.place_search(place(query="咖啡", anchor_source="named", near="外滩", city="上海")))
        assert error.code == "ambiguous_location" and error.argument == "near_candidate"
        assert [(c.district, c.level) for c in error.candidates] == [("浦东新区", "住宅区"), ("黄浦区", "兴趣点")]
        result = await svc.place_search(
            place(query="咖啡", anchor_source="named", near="外滩", city="上海", near_candidate=2)
        )
        assert result["anchor"] == {"source": "named", "label": "外滩", "city": "上海市"}
        assert fake.params("amap", AROUND)[0]["location"] == "121.492308,31.233383"

    async def test_current_location_needs_the_device(self):
        fake = FakeUpstream()
        error = await failure(service(fake).place_search(place(query="咖啡", anchor_source="current_location")))
        assert error.code == "location_unavailable" and fake.calls == []

    async def test_slow_upstream_hits_the_call_deadline(self):
        fake = FakeUpstream().on("amap", TEXT, Delay(5, AMAP_EMPTY))
        error = await failure(service(fake, call_timeout_seconds=0.2).place_search(place(query="火锅")))
        assert error.code == "timeout"


class TestRouteCompare:
    async def test_named_places_in_one_city(self):
        fake = (
            FakeUpstream()
            .on("amap", GEO, HONGQIAO_GEO, address="虹桥火车站")
            .sample("amap", "geo_bund", address="外滩")
        )
        for mode in ("driving", "transit", "walking"):
            fake.sample("amap", mode)
        svc = service(fake)
        args = route(
            origin="虹桥火车站",
            origin_city="上海",
            destination="外滩",
            destination_candidate=2,
            modes=["driving", "transit", "walking"],
            requested_departure_time="明天  早上 8 点",
        )
        result = await svc.route_compare(args)
        assert result["status"] == "success" and result["providers"] == ["amap"]
        assert result["origin"] == {"source": "named", "label": "虹桥火车站", "city": "上海市"}
        assert [r["mode"] for r in result["routes"]] == ["driving", "transit", "walking"]
        transit = result["routes"][1]
        assert transit["fare_yuan"] == 5 and "distance_m" not in transit and len(transit["alternatives"]) == 2
        assert result["requested_departure_time"] == "明天 早上 8 点"
        assert result["limitations"] == ["departure_time_not_applied"]
        # 终点没给城市：按起点的城市挑（外滩在上海有两个候选，模型指定了第 2 个）
        assert fake.params("amap", GEO)[1] == {"address": "外滩"}
        (transit_params,) = fake.params("amap", "/v3/direction/transit/integrated")
        assert transit_params["city"] == transit_params["cityd"] == "上海市"

    async def test_place_ids_from_search(self):
        fake = FakeUpstream().sample("amap", "place_detail").sample("tmap", "detail").sample("amap", "walking")
        args = route(
            origin="海底捞火锅(外滩店)",
            origin_place_id="amap_B0FFKKRXAZ",
            destination="外滩",
            destination_place_id="tmap_10394124075561511585",
            modes=["walking"],
        )
        result = await service(fake).route_compare(args)
        assert result["providers"] == ["amap", "tmap"]
        assert fake.params("amap", "/v3/direction/walking")[0] == {
            "origin": "121.487590,31.238251",
            "destination": "121.493491,31.232091",
        }

    async def test_place_id_falls_back_to_the_name_when_its_provider_is_down(self):
        fake = FakeUpstream().on("amap", "/v5/place/detail", AMAP_QUOTA).on("amap", GEO, AMAP_QUOTA)
        fake.sample("tmap", "suggestion").sample("tmap", "detail").sample("amap", "walking")
        args = route(
            origin="外滩",
            origin_place_id="amap_B0FFKKRXAZ",
            origin_city="上海",
            destination="外滩",
            destination_place_id="tmap_10394124075561511585",
            modes=["walking"],
        )
        result = await service(fake).route_compare(args)
        assert result["routes"][0]["provider"] == "amap"
        assert fake.paths("tmap") == [SUGGEST, "/ws/place/v1/detail"]

    async def test_unknown_place_id(self):
        fake = FakeUpstream().on("amap", "/v5/place/detail", AMAP_EMPTY)
        args = route(origin="某店", origin_place_id="amap_B000", destination="外滩", destination_candidate=1)
        assert (await failure(service(fake).route_compare(args))).code == "place_id_unresolved"

    async def test_current_location_and_transit_without_city_goes_to_tmap(self):
        fake = FakeUpstream().sample("amap", "convert").on("amap", "/v3/geocode/regeo", 500)
        fake.on("tmap", "/ws/geocoder/v1/", 500).on("amap", GEO, HONGQIAO_GEO).sample("tmap", "transit")
        fake.sample("amap", "driving")
        args = route(
            origin="current_location",
            origin_source="current_location",
            destination="虹桥火车站",
            modes=["transit", "driving"],
        )
        result = await service(fake).route_compare(args, DEVICE)
        assert result["origin"] == {"source": "current_location"}
        assert [(r["mode"], r["provider"]) for r in result["routes"]] == [("transit", "tmap"), ("driving", "amap")]
        assert "/v3/direction/transit/integrated" not in fake.paths("amap")

    async def test_location_unavailable(self):
        args = route(origin="当前位置", origin_source="current_location", destination="外滩")
        assert (await failure(service(FakeUpstream()).route_compare(args))).code == "location_unavailable"

    async def test_too_far_is_no_route_and_does_not_move(self):
        fake = FakeUpstream().on("amap", GEO, HONGQIAO_GEO, address="虹桥火车站")
        fake.on("amap", GEO, amap_geo(("116.397,39.908", "北京市", "东城区", "兴趣点")), address="天安门")
        fake.sample("amap", "walking_far").on("amap", "/v5/direction/bicycling", {"status": "0", "infocode": "20803"})
        args = route(origin="虹桥火车站", destination="天安门", modes=["walking", "bicycling"])
        assert (await failure(service(fake).route_compare(args))).code == "no_route"
        assert fake.paths("tmap") == []

    async def test_slow_mode_is_reported_and_others_kept(self):
        fake = FakeUpstream().on("amap", GEO, HONGQIAO_GEO)
        fake.sample("amap", "driving").on("amap", "/v3/direction/walking", Delay(5, {}))
        svc = service(fake, call_timeout_seconds=0.3)
        result = await svc.route_compare(
            route(origin="虹桥火车站", destination="虹桥火车站", modes=["driving", "walking"])
        )
        assert result["status"] == "degraded" and result["unavailable_modes"] == ["walking"]
        assert [r["mode"] for r in result["routes"]] == ["driving"]

    async def test_ambiguous_endpoint_via_tmap_and_not_found(self):
        fake = FakeUpstream().on("amap", GEO, 500).sample("tmap", "suggestion_gulou")
        error = await failure(service(fake).route_compare(route(origin="鼓楼", destination="鼓楼")))
        assert error.code == "ambiguous_location" and error.argument == "origin_candidate"
        assert len(error.candidates) == 5
        fake = FakeUpstream().sample("amap", "geo_none").on("amap", TEXT, AMAP_EMPTY)
        error = await failure(service(fake).route_compare(route(origin="嗯嗯嗯", destination="外滩")))
        assert (error.code, error.place_name) == ("place_not_found", "嗯嗯嗯")
        assert fake.paths("tmap") == []

    async def test_name_selection_from_search_when_geocode_has_nothing(self):
        fake = FakeUpstream().sample("amap", "geo_none").sample("amap", "place_text").sample("amap", "walking")
        args = route(origin="海底捞火锅外滩店", origin_city="上海", destination="海底捞火锅外滩店", modes=["walking"])
        result = await service(fake).route_compare(args)
        assert result["origin"]["city"] == "上海市"


class TestArguments:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"query": " "},
            {"query": "x" * 81},
            {"query": "咖啡", "anchor_source": "named"},
            {"query": "咖啡", "anchor_source": "none", "near": "外滩"},
            {"query": "咖啡", "anchor_source": "named", "near": "121.49,31.24"},
            {"query": "咖啡", "near_candidate": 1},
            {"query": "咖啡", "radius_m": 50},
            {"query": "咖啡", "limit": 6},
            {"query": "api_key=abcdef123456"},
        ],
    )
    def test_place_arguments(self, kwargs):
        with pytest.raises(ToolFailure) as caught:
            place(**kwargs)
        assert caught.value.code == "invalid_arguments"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"origin": "current_location", "destination": "外滩"},
            {"origin": "外滩", "destination": "外滩", "origin_source": "current_location"},
            {"origin": "外滩", "destination": "外滩", "modes": []},
            {"origin": "外滩", "destination": "外滩", "modes": ["driving", "driving"]},
            {"origin": "外滩", "destination": "外滩", "modes": ["flying"]},
            {"origin": "外滩", "destination": "外滩", "origin_place_id": "B0FFKKRXAZ"},
            {"origin": "外滩", "destination": "外滩", "origin_place_id": "amap_B1", "origin_candidate": 1},
            {"origin": "外滩", "destination": "121.49,31.24"},
        ],
    )
    def test_route_arguments(self, kwargs):
        with pytest.raises(ToolFailure) as caught:
            route(**kwargs)
        assert caught.value.code == "invalid_arguments"

    def test_current_location_words(self):
        args = route(origin="当前位置", origin_source="current_location", destination="外滩")
        assert args.origin == "current_location"
