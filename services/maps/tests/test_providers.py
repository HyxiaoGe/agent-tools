"""两家客户端：从录好的样本里取字段、错误怎么分类、请求怎么拼。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest

from app import amap, tmap
from app.amap import AmapClient
from app.main import configure_logging
from app.model import Endpoint, Point
from app.tmap import TmapClient
from app.upstream import BEIJING, Gate, QuotaMemo, UpstreamError, Usage
from tests.fake import AMAP_QUOTA, KEYS, TMAP_QUOTA, FakeUpstream, sample

pytestmark = pytest.mark.anyio

BUND = Point(121.49098, 31.240631)
HONGQIAO = Point(121.320, 31.194)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def clients(fake: FakeUpstream) -> tuple[AmapClient, TmapClient, Usage]:
    usage, memo, http = Usage(), QuotaMemo(), fake.client()
    return (
        AmapClient(KEYS["amap"], http, Gate("amap", memo, usage)),
        TmapClient(KEYS["tmap"], http, Gate("tmap", memo, usage)),
        usage,
    )


class TestAmapParsing:
    def test_search_keeps_documented_fields_and_official_photo(self):
        first = amap.parse_places(sample("amap", "place_text")["body"])[0]
        assert first.place_id == "amap_B0FFKKRXAZ"
        assert (first.name, first.city, first.district) == ("海底捞火锅(外滩店)", "上海市", "黄浦区")
        assert (first.rating, first.reference_cost_yuan, first.business_area) == (4.7, 120, "北京路")
        assert first.open_hours == "周一至周日 11:00-07:00"
        assert first.photo_url.startswith("https://store.is.autonavi.com/")
        assert first.distance_m is None
        around = amap.parse_places(sample("amap", "place_around")["body"])
        assert around[0].distance_m == 54 and len(around) == 10

    def test_empty_values_and_foreign_photos_are_dropped(self):
        raw = {
            "id": "B1",
            "name": "店",
            "location": "121.1,31.1",
            "address": [],
            "business": {"rating": [], "cost": "-1", "opentime_week": "token=abcdef123"},
            "photos": [{"url": "http://store.is.autonavi.com/a"}, {"url": "https://evil.example/a"}],
        }
        place = amap.parse_place(raw)
        assert place.address is None and place.rating is None and place.reference_cost_yuan is None
        assert place.open_hours is None  # 像密钥的文字整条不要
        assert place.photo_url is None

    def test_geocodes_keep_cities_and_levels(self):
        candidates = amap.parse_geocodes(sample("amap", "geo_gulou")["body"])
        assert [c.city for c in candidates] == ["开封市", "福州市", "徐州市", "南京市"]
        assert {c.level for c in candidates} == {"区县"}

    def test_routes(self):
        driving = amap.parse_route(sample("amap", "driving")["body"], "driving")
        assert (driving.distance_m, driving.duration_s, driving.summary, driving.toll_yuan) == (
            28251,
            6181,
            "速度最快",
            0,
        )
        walking = amap.parse_route(sample("amap", "walking")["body"], "walking")
        assert (walking.distance_m, walking.duration_s, walking.toll_yuan) == (817, 654, None)
        transit = amap.parse_route(sample("amap", "transit")["body"], "transit")
        assert transit.distance_m is None  # 公交不给总距离
        assert (transit.duration_s, transit.fare_yuan, transit.transit_type, transit.transfers) == (
            3380,
            5,
            "subway",
            0,
        )
        subway = transit.legs[1]
        assert (subway.line_name, subway.departure_stop, subway.arrival_stop) == (
            "地铁10号线(虹桥火车站--基隆路)",
            "虹桥火车站",
            "南京东路",
        )
        assert (subway.via_stop_count, subway.entrance, subway.exit) == (14, "南1入口", "6号口")
        assert len(transit.alternatives) == 2 and transit.alternatives[0].alternatives == []

    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            ("10003", "quota"),
            ("10044", "quota"),
            ("10004", "rate"),
            ("10021", "rate"),
            ("10001", "auth"),
            ("10009", "auth"),
            ("10016", "unavailable"),
            ("20000", "rejected"),
            ("20803", "rejected"),
            ("30001", "rejected"),
            ("", "unavailable"),
        ],
    )
    def test_classify(self, code, kind):
        assert amap.classify(code) == kind

    def test_quota_resets_on_the_first_of_next_month_beijing(self):
        assert amap.next_reset(datetime(2026, 12, 31, 20, 0, tzinfo=UTC)) == datetime(2027, 2, 1, tzinfo=BEIJING)
        assert amap.next_reset(datetime(2026, 12, 31, 15, 0, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=BEIJING)


class TestTmapParsing:
    def test_search_and_suggestion_shapes(self):
        searched = tmap.parse_places(sample("tmap", "search_region")["body"])[0]
        assert (searched.city, searched.district, searched.category) == ("上海市", "闵行区", "美食:火锅")
        suggested = tmap.parse_places(sample("tmap", "suggestion")["body"])[0]
        assert suggested.place_id == "tmap_10394124075561511585"
        assert (suggested.name, suggested.city, suggested.district) == ("外滩", "上海市", "黄浦区")
        near = tmap.parse_places(sample("tmap", "search_nearby_3000")["body"])[0]
        assert near.distance_m == 53 and near.rating is None and near.photo_url is None

    def test_name_candidates_only_count_the_place_itself(self):
        bund = tmap.parse_places(sample("tmap", "suggestion")["body"])
        assert [(c.city, c.district) for c in tmap.name_candidates(bund, "外滩")] == [("上海市", "黄浦区")]
        gulou = tmap.parse_places(sample("tmap", "suggestion_gulou")["body"])
        cities = [c.city for c in tmap.name_candidates(gulou, "鼓楼")]
        assert cities == ["北京市", "宁波市", "邯郸市", "酒泉市", "杭州市"]  # “宁波鼓楼”“磁县鼓楼”去掉前缀算鼓楼

    def test_routes_are_converted_to_seconds_and_yuan(self):
        driving = tmap.parse_route(sample("tmap", "driving")["body"], "driving")
        assert (driving.distance_m, driving.duration_s, driving.toll_yuan) == (21740, 3060, 0)
        transit = tmap.parse_route(sample("tmap", "transit")["body"], "transit")
        assert (transit.duration_s, transit.fare_yuan, transit.transit_type, transit.walking_distance_m) == (
            3300,
            5,
            "subway",
            1134,
        )
        subway = transit.legs[1]
        # 腾讯 station_count 11，换成和高德一样的途经站数
        assert (subway.line_name, subway.via_stop_count, subway.entrance, subway.exit) == (
            "地铁2号线",
            10,
            "北2入口",
            "6口",
        )
        assert subway.duration_s == 2040

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (121, "quota"),
            (120, "rate"),
            (190, "auth"),
            (311, "auth"),
            (373, "rejected"),
            (347, "rejected"),
            (500, "unavailable"),
        ],
    )
    def test_classify(self, status, kind):
        assert tmap.classify(status) == kind

    def test_quota_resets_at_next_beijing_midnight(self):
        assert tmap.next_reset(datetime(2026, 11, 4, 16, 30, tzinfo=UTC)) == datetime(2026, 11, 6, tzinfo=BEIJING)


class TestRequests:
    async def test_amap_requests_carry_fields_and_cities(self):
        fake = FakeUpstream().sample("amap", "place_text").sample("amap", "transit").sample("amap", "geo_gulou")
        client, _, _ = clients(fake)
        await client.text_search(["火锅", "烤肉"], "上海")
        (params,) = fake.params("amap", "/v5/place/text")
        assert params == {
            "keywords": "火锅|烤肉",
            "show_fields": "business,photos",
            "page_size": "10",
            "region": "上海",
            "city_limit": "true",
        }
        origin, destination = Endpoint("a", HONGQIAO, "上海市"), Endpoint("b", BUND, "上海市")
        await client.route("transit", origin, destination)
        (params,) = fake.params("amap", "/v3/direction/transit/integrated")
        assert params["city"] == params["cityd"] == "上海市" and params["origin"] == "121.320000,31.194000"
        lookup = await client.geocode("鼓楼", None)
        assert len(lookup.candidates) == 4 and lookup.places is None

    async def test_amap_transit_without_cities_is_left_to_tmap_without_a_request(self):
        fake = FakeUpstream()
        client, _, _ = clients(fake)
        with pytest.raises(UpstreamError) as caught:
            await client.route("transit", Endpoint("a", HONGQIAO, None), Endpoint("b", BUND, "上海市"))
        assert caught.value.kind == "unsupported" and fake.calls == []

    async def test_tmap_searches_only_the_first_term_and_uses_lat_lng(self):
        fake = (
            FakeUpstream()
            .sample("tmap", "search_region")
            .sample("tmap", "search_nearby_3000")
            .sample("tmap", "suggestion")
        )
        _, client, _ = clients(fake)
        await client.text_search(["火锅", "烤肉"], "上海(市)")
        await client.around_search(["咖啡", "奶茶"], BUND, 3000)
        await client.text_search(["外滩"], None)  # 没城市用输入提示
        region, nearby = fake.params("tmap", "/ws/place/v1/search")
        assert region["keyword"] == "火锅" and region["boundary"] == "region(上海市,0)"
        assert nearby["boundary"] == "nearby(31.240631,121.490980,3000,0)" and nearby["orderby"] == "_distance"
        (suggest,) = fake.params("tmap", "/ws/place/v1/suggestion/")
        assert suggest == {"keyword": "外滩", "page_size": "10"}

    async def test_tmap_geocode_reuses_the_suggestion_batch(self):
        fake = FakeUpstream().sample("tmap", "suggestion")
        _, client, _ = clients(fake)
        lookup = await client.geocode("外滩", "上海")
        assert len(lookup.candidates) == 1 and len(lookup.places) == 10
        assert fake.params("tmap", "/ws/place/v1/suggestion/") == [
            {"keyword": "外滩", "page_size": "10", "region": "上海", "region_fix": "1"}
        ]

    async def test_convert_gps(self):
        fake = FakeUpstream().sample("amap", "convert").sample("tmap", "translate")
        amap_client, tmap_client, _ = clients(fake)
        assert await amap_client.convert_gps(121.4855, 31.242) == Point(121.489983995226, 31.240029568143)
        assert await tmap_client.convert_gps(121.4855, 31.242) == Point(121.489984, 31.24003)
        assert fake.params("tmap", "/ws/coord/v1/translate") == [{"locations": "31.242000,121.485500", "type": "1"}]


class TestErrors:
    @pytest.mark.parametrize(
        ("response", "kind"),
        [
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("down"), "unavailable"),
            (429, "rate"),
            (502, "unavailable"),
            ("not json", "invalid_response"),
            ([1, 2], "invalid_response"),
            (sample("amap", "bad_key_v3")["body"], "auth"),
            (sample("amap", "walking_far")["body"], "rejected"),
            (AMAP_QUOTA, "quota"),
        ],
    )
    async def test_amap(self, response, kind):
        fake = FakeUpstream().on("amap", "/v3/direction/walking", response)
        client, _, _ = clients(fake)
        with pytest.raises(UpstreamError) as caught:
            await client.route("walking", Endpoint("a", HONGQIAO), Endpoint("b", BUND))
        assert caught.value.kind == kind
        if kind == "quota":
            assert caught.value.reset_at is not None and caught.value.reset_at.day == 1

    @pytest.mark.parametrize(
        ("response", "kind"),
        [
            (TMAP_QUOTA, "quota"),
            (sample("tmap", "bad_key")["body"], "auth"),
            (sample("tmap", "walking_far")["body"], "rejected"),
            ({"status": "0"}, "invalid_response"),
            (httpx.ReadTimeout("slow"), "timeout"),
        ],
    )
    async def test_tmap(self, response, kind):
        fake = FakeUpstream().on("tmap", "/ws/direction/v1/walking/", response)
        _, client, _ = clients(fake)
        with pytest.raises(UpstreamError) as caught:
            await client.route("walking", Endpoint("a", HONGQIAO), Endpoint("b", BUND))
        assert caught.value.kind == kind

    async def test_auth_failure_is_logged_without_the_key(self, caplog):
        configure_logging()  # 服务启动时就这样配：httpx 的请求行带 key，压到 WARNING
        caplog.set_level(logging.DEBUG)
        fake = FakeUpstream().sample("amap", "bad_key_v3").sample("tmap", "bad_key")
        amap_client, tmap_client, usage = clients(fake)
        for client in (amap_client, tmap_client):
            with pytest.raises(UpstreamError):
                await client.reverse_city(HONGQIAO)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == [
            "maps_auth_failed provider=amap api=regeocode code=10001",
            "maps_auth_failed provider=tmap api=regeocode code=190",
        ]
        assert KEYS["amap"] not in caplog.text and KEYS["tmap"] not in caplog.text
        summary = usage.summary(datetime.now(BEIJING).date())
        assert [(p["provider"], p["requests"], p["failed"]) for p in summary["providers"]] == [
            ("amap", 1, 1),
            ("tmap", 1, 1),
        ]

    async def test_empty_results_are_not_errors(self):
        fake = FakeUpstream().on("amap", "/v5/place/text", {"status": "1", "count": "0", "pois": []})
        client, _, usage = clients(fake)
        assert await client.text_search(["不存在"], "上海") == []
        assert usage.summary(datetime.now(BEIJING).date())["providers"][0]["failed"] == 0
