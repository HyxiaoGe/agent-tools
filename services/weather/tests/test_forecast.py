"""get_weather 的逻辑（app/forecast.py）和 MCP 工具（app/mcp_server.py）。

用例清单照 Fusion 的 test/test_qweather_forecast.py 和灵稿的 tests/test_weather.py 搬过来，
再加上当地今天按时区算的几个。
和风用 MockTransport 模拟（原始字段），时间用固定的“现在”。
"""

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastmcp import Client

from app.forecast import (
    FORECAST_CACHE_SECONDS,
    Forecaster,
    ForecastError,
    city_label,
    parse_arguments,
    select_city,
    summarize_hourly,
)
from app.main import create_app
from app.mcp_server import DESCRIPTION, build_mcp
from app.qweather import City, QWeatherClient
from tests.test_main import credentials, settings

pytestmark = pytest.mark.anyio

# 北京时间 2026-10-08 16:00
NOW = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def city(name, adm2="", adm1="", country="中国", rank=15, lid=None, tz="Asia/Shanghai") -> City:
    return City(
        location_id=lid or f"id-{country}{adm1}{adm2}{name}",
        name=name,
        adm1=adm1,
        adm2=adm2,
        country=country,
        rank=rank,
        tz=tz,
        utc_offset="",
    )


def raw_city(c: City) -> dict:
    return {
        "id": c.location_id,
        "name": c.name,
        "adm1": c.adm1,
        "adm2": c.adm2,
        "country": c.country,
        "rank": str(c.rank),
        "tz": c.tz,
        "utcOffset": "+08:00",
    }


HANGZHOU = city("杭州", "杭州", "浙江省", lid="101210101")


def raw_daily(first_day: int = 8, count: int = 15) -> list[dict]:
    return [
        {
            "fxDate": f"2026-10-{day:02d}",
            "textDay": "晴",
            "textNight": "多云",
            "tempMax": "26",
            "tempMin": "17.5",
            "windDirDay": "东风",
            "windDirNight": "北风",
            "windScaleDay": "1-3",
            "windScaleNight": "3-4",
        }
        for day in range(first_day, first_day + count)
    ]


# 从 10-08 16:00 起 168 小时：今天 16–23 时 8 个，明天到 10-14 每天 24 个，10-15 0–15 时 16 个；每天 19–21 时下小雨
RAW_HOURLY = [
    {
        "fxTime": f"2026-10-{8 + (16 + i) // 24:02d}T{(16 + i) % 24:02d}:00+08:00",
        "temp": str(20 - i % 24 // 4),
        "text": "小雨" if 3 <= i % 24 <= 5 else "多云",
        "pop": "70" if 3 <= i % 24 <= 5 else "10",
        "precip": "0.6" if 3 <= i % 24 <= 5 else "0.0",
    }
    for i in range(168)
]


class FakeQWeather:
    """模拟和风：按路径回原始字段，记下请求；可以让某个路径失败。"""

    def __init__(self, cities=(HANGZHOU,), daily=None, hourly=None, fail: dict[str, int] | None = None) -> None:
        self.cities = [raw_city(c) for c in cities]
        self.daily = raw_daily() if daily is None else daily
        self.hourly = RAW_HOURLY if hourly is None else hourly
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            name = request.url.path.rsplit("/", 1)[-1]
            if name in self.fail:
                return httpx.Response(self.fail[name], json={"code": str(self.fail[name])})
            if name == "lookup":
                return httpx.Response(200, json={"code": "200", "location": self.cities})
            if name == "15d":
                return httpx.Response(200, json={"code": "200", "daily": self.daily})
            return httpx.Response(200, json={"code": "200", "hourly": self.hourly})

        return httpx.MockTransport(handle)


def forecaster(fake: FakeQWeather, now: Any = NOW, clock: Any = None) -> Forecaster:
    client = QWeatherClient(credentials(), transport=fake.transport())
    return Forecaster(lambda: client, now=(now if callable(now) else lambda: now), clock=clock or (lambda: 0.0))


# ---- 参数、挑城市、写法 ----


def test_arguments_take_place_names_and_one_exact_date():
    assert parse_arguments(" 深圳市 ", None) == ("深圳市", None)
    assert parse_arguments("深圳市", "") == ("深圳市", None)
    assert parse_arguments("深圳市", "2026-10-20") == ("深圳市", date(2026, 10, 20))
    for location, requested in (
        ("", None),
        ("   ", None),
        ("x" * 121, None),
        (42, None),
        ("114.05,22.54", None),
        ("440300", None),
        ("深圳市", "2026-02-30"),
        ("深圳市", "20261020"),
        ("深圳市", "下周三"),
        ("深圳市", 20261020),
    ):
        with pytest.raises(ForecastError) as e:
            parse_arguments(location, requested)
        assert e.value.code == "invalid_arguments" and e.value.detail


def test_resolution_rules():
    hangzhou, xiaoshan = city("杭州", "杭州", "浙江省"), city("萧山", "杭州", "浙江省")
    chaoyang_ln, chaoyang_bj = city("朝阳", "朝阳", "辽宁省"), city("朝阳", "北京", "北京市")
    beijing, haidian = city("北京", "北京", "北京市"), city("海淀", "北京", "北京市")
    xihu_hz, xihu_nc = city("西湖", "杭州", "浙江省"), city("西湖", "南昌", "江西省")
    guangzhou, panyu = city("广州", "广州", "广东省"), city("番禺", "广州", "广东省")
    xinbei_tw, xinbei_cz = city("新北市", "新北市", "台湾省", rank=85), city("新北", "常州", "江苏省", rank=35)

    assert select_city("杭州", [hangzhou, xiaoshan]) is hangzhou
    assert select_city("杭州市", [hangzhou, xiaoshan]) is hangzhou
    assert select_city("广州番禺", [guangzhou, panyu]) is panyu  # 写在后面的更具体
    assert select_city("北京朝阳", [chaoyang_ln, chaoyang_bj, beijing]) is chaoyang_bj
    assert select_city("北京市海淀区中关村", [haidian, beijing, chaoyang_bj]) is haidian
    assert select_city("杭州西湖", [xihu_hz, xihu_nc]) is xihu_hz
    assert select_city("新北市", [xinbei_cz, xinbei_tw]) is xinbei_tw  # 国内的 rank 高也照样认
    assert select_city(city_label(xihu_nc), [xihu_hz, xihu_nc]) is xihu_nc  # 模型照候选的写法再查
    twin = city("广州", "广州", "广东省", lid="other")
    assert select_city("广州", [guangzhou, twin]) is guangzhou  # 写法一模一样的重复条目：取第一个
    minor = city("广州", "Y", "X", country="Nowhere", rank=85)
    assert select_city("广州", [minor, guangzhou]) is guangzhou
    # 同名分不出时，排名比最前的落后超过 10 的不要（排名是 11-04 在 dev 上查的）
    paris = city("巴黎", "巴黎", "法兰西岛大区", country="法国", rank=20)
    paris_ky = city("巴黎", "波旁县", "Kentucky", country="美国", rank=75)
    assert select_city("巴黎", [paris, paris_ky]) is paris
    dc = city("华盛顿", "华盛顿", "哥伦比亚特区", country="美国", rank=30)
    others = [
        city("华盛顿", "桑德兰", "英格兰", country="英国", rank=55),
        city("华盛顿", "华盛顿县", "犹他州", country="美国", rank=62),
    ]
    assert select_city("华盛顿", [*others, dc]) is dc

    with pytest.raises(ForecastError) as e:
        select_city("朝阳", [chaoyang_ln, chaoyang_bj])
    assert e.value.code == "ambiguous_location" and e.value.candidates == ["辽宁省·朝阳", "北京市·北京·朝阳"]
    # 差得不多的照样问；落后多的不进候选
    chaoyang_cc = city("朝阳", "长春", "吉林省", rank=35)
    with pytest.raises(ForecastError) as e:
        select_city("朝阳", [city("朝阳", "朝阳", "辽宁省", rank=13), chaoyang_bj, chaoyang_cc])
    assert e.value.candidates == ["辽宁省·朝阳", "北京市·北京·朝阳"]
    # 有没给排名的就不比
    with pytest.raises(ForecastError) as e:
        select_city("巴黎", [paris, city("巴黎", "波旁县", "Kentucky", country="美国", rank=None)])
    assert e.value.code == "ambiguous_location"

    for query, cities in (
        ("乌镇", [city("乌镇", "厄尔-卢瓦省", "中央-卢瓦尔河谷大区", country="法国", rank=83)]),
        ("不存在的地方xyz", [city("Shahumyan", "x", "y", country="亚美尼亚", rank=85)]),
        ("湖南", [city("长沙", "长沙", "湖南省")]),  # 模糊查询给的城市名字不在地名里：不认
    ):
        with pytest.raises(ForecastError) as e:
            select_city(query, cities)
        assert e.value.code == "location_not_found"


def test_labels_include_country_only_abroad():
    assert city_label(city("杭州", "杭州", "浙江省")) == "浙江省·杭州"
    assert city_label(city("西湖", "杭州", "浙江省")) == "浙江省·杭州·西湖"
    assert city_label(city("东京", "东京", "东京都", country="日本")) == "日本·东京都·东京"


def test_hourly_summary_gives_the_peak_and_the_wet_hours():
    def h(hour, temp, pop=None, precip=None):
        point = {"time": f"2026-11-01T{hour:02d}:00:00+08:00", "temp_c": temp, "weather": "晴"}
        return point | ({"pop": pop} if pop is not None else {}) | ({"precip_mm": precip} if precip is not None else {})

    # 和风真会给出“晴、0 毫米、降水概率六成”这种：高峰照样报出来，有没有降水量另算
    dry = [h(9, 26, 19, 0.0), h(10, 28, 61, 0.0), h(11, 30, 61, 0.0), h(12, 30)]
    assert summarize_hourly(dry) == {
        "window": "2026-11-01T09:00:00+08:00 ~ 2026-11-01T12:00:00+08:00",
        "temp_min_c": 26,
        "temp_max_c": 30,
        "max_pop_percent": 61,
        "max_pop_time": "2026-11-01T10:00:00+08:00",
        "precipitation_hours": 0,
    }
    wet = summarize_hourly([h(14, 25, 70, 1.2), h(15, 24, 80, 0.36), h(16, 24, 40, 0.0), h(17, 24, 30, 0.2)])
    assert wet["precipitation_hours"] == 3 and wet["total_precip_mm"] == 1.8
    assert (wet["first_precipitation_time"][11:16], wet["last_precipitation_time"][11:16]) == ("14:00", "17:00")
    assert summarize_hourly([]) is None


# ---- 查预报 ----


async def test_named_place_returns_four_days_from_local_today():
    fake = FakeQWeather()
    result = await forecaster(fake).forecast("杭州")

    assert result["ok"] and result["resolved_location"] == "浙江省·杭州" and result["local_today"] == "2026-10-08"
    assert result["day_count"] == 4 and [d["date"] for d in result["forecast_days"]][0] == "2026-10-08"
    assert result["forecast_days"][0] == {
        "date": "2026-10-08",
        "weekday": "Thursday",
        "day_weather": "晴",
        "night_weather": "多云",
        "high_c": 26,
        "low_c": 17.5,
        "day_wind_direction": "东风",
        "day_wind_power": "1-3级",
        "night_wind_direction": "北风",
        "night_wind_power": "3-4级",
    }
    assert len(result["hourly"]) == 24 and result["hourly"][0]["time"] == "2026-10-08T16:00:00+08:00"
    assert result["hourly_summary"]["max_pop_percent"] == 70 and result["hourly_summary"]["precipitation_hours"] == 3
    assert result["fetched_at"] == "2026-10-08T16:00:00+08:00"  # 北京时间
    assert result["limitations"] == [] and "requested_date" not in result
    assert fake.paths() == ["lookup", "15d", "168h"]
    assert fake.requests[0].url.params["lang"] == "zh"
    assert fake.requests[1].url.params["location"] == "101210101"


async def test_bad_days_and_hours_are_dropped():
    daily = raw_daily(7)  # 10-07（昨天）起
    daily[2] = {**daily[2], "tempMax": "?"}  # 10-09 气温不对
    daily.insert(3, dict(daily[1]))  # 10-08 重复
    hourly = [RAW_HOURLY[0], {**RAW_HOURLY[1], "fxTime": "2026-10-08T17:00"}, RAW_HOURLY[0], RAW_HOURLY[2]]
    result = await forecaster(FakeQWeather(daily=daily, hourly=hourly)).forecast("杭州")
    assert [d["date"] for d in result["forecast_days"]] == ["2026-10-08", "2026-10-10", "2026-10-11", "2026-10-12"]
    # 没带时区的、时间往回走的都不要
    assert [p["time"][11:16] for p in result["hourly"]] == ["16:00", "18:00"]


async def test_requested_date_in_window_returns_only_that_day():
    # 逐小时从 10-08 16:00 起：今天还剩 8 个小时，之后 7 天内的哪天都是整天（11-04 Sean：问明天就画明天一整天）
    for requested, count, first, last in (
        ("2026-10-08", 8, "2026-10-08T16:00:00+08:00", "2026-10-08T23:00:00+08:00"),
        ("2026-10-09", 24, "2026-10-09T00:00:00+08:00", "2026-10-09T23:00:00+08:00"),
        ("2026-10-14", 24, "2026-10-14T00:00:00+08:00", "2026-10-14T23:00:00+08:00"),
    ):
        result = await forecaster(FakeQWeather()).forecast("杭州", date.fromisoformat(requested))
        assert result["requested_date"] == requested and result["day_count"] == 1
        assert [d["date"] for d in result["forecast_days"]] == [requested]
        assert len(result["hourly"]) == count and result["hourly_summary"]["window"] == f"{first} ~ {last}"
        assert result["limitations"] == [] and "forecast_range" not in result


async def test_requested_date_beyond_seven_days_skips_hourly():
    for requested in (date(2026, 10, 15), date(2026, 10, 22)):  # 第 8 天、第 15 天（最后一天）
        fake = FakeQWeather()
        result = await forecaster(fake).forecast("杭州", requested)
        assert [d["date"] for d in result["forecast_days"]] == [requested.isoformat()]
        assert "hourly" not in result and "hourly_summary" not in result and result["limitations"] == []
        assert fake.paths() == ["lookup", "15d"]  # 省一次和风调用


async def test_requested_date_outside_window_gives_no_days_only_the_range():
    # 11-04 Sean：查不到的那天别拿别的日子凑。给了整段，模型照样会列出来
    for requested in (date(2026, 10, 23), date(2026, 10, 7)):  # 第 16 天、昨天
        fake = FakeQWeather()
        result = await forecaster(fake).forecast("杭州", requested)
        assert result["day_count"] == 0 and result["forecast_days"] == []
        assert result["requested_date"] == requested.isoformat() and "hourly" not in result
        assert result["forecast_range"] == {"first_date": "2026-10-08", "last_date": "2026-10-22"}
        assert result["limitations"] == ["requested_date_not_covered"]
        assert fake.paths() == ["lookup", "15d"]


async def test_hourly_failure_keeps_daily_forecast():
    for fake in (FakeQWeather(fail={"168h": 500}), FakeQWeather(hourly=[])):
        result = await forecaster(fake).forecast("杭州")
        assert result["day_count"] == 4 and "hourly" not in result
        assert result["limitations"] == ["hourly_unavailable"]


async def test_fewer_days_are_marked():
    result = await forecaster(FakeQWeather(daily=raw_daily()[:2])).forecast("杭州")
    assert result["day_count"] == 2 and result["limitations"] == ["fewer_days"]
    with pytest.raises(ForecastError) as e:
        await forecaster(FakeQWeather(daily=[])).forecast("杭州")
    assert e.value.code == "invalid_response"


async def test_local_today_follows_the_place_time_zone():
    # 北京时间 10-09 08:00，纽约还是 10-08 晚上；取不到逐小时也不能丢掉当地今天
    new_york = city("纽约", "纽约", "纽约州", country="美国", rank=10, tz="America/New_York")
    now = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
    fake = FakeQWeather(cities=[new_york], fail={"168h": 500})
    result = await forecaster(fake, now=now).forecast("纽约")
    assert result["local_today"] == "2026-10-08" and result["forecast_days"][0]["date"] == "2026-10-08"
    assert result["fetched_at"] == "2026-10-09T08:00:00+08:00"

    # 时区名认不出时用当前偏移，再不行按北京时间
    odd = city("某地", "某地", "某州", country="某国", rank=10, tz="Nowhere/Odd")
    fake = FakeQWeather(cities=[odd])
    fake.cities[0]["utcOffset"] = "-05:00"
    result = await forecaster(fake, now=now).forecast("某地")
    assert result["local_today"] == "2026-10-08"


async def test_cache_crossing_midnight_drops_yesterday():
    # 10-08 23:50 查过、缓存里的逐日从 10-08 起；10-09 00:10 再查（缓存还没过期）应该从 10-09 起
    t, clock = [datetime(2026, 10, 8, 15, 50, tzinfo=UTC)], [0.0]
    fake = FakeQWeather()
    weather = forecaster(fake, now=lambda: t[0], clock=lambda: clock[0])
    first = await weather.forecast("杭州")
    t[0], clock[0] = datetime(2026, 10, 8, 16, 10, tzinfo=UTC), 20 * 60
    second = await weather.forecast("杭州")
    assert first["forecast_days"][0]["date"] == "2026-10-08"
    assert second["local_today"] == "2026-10-09" and second["forecast_days"][0]["date"] == "2026-10-09"
    assert fake.paths() == ["lookup", "15d", "168h", "lookup"]  # 第二次预报走缓存
    assert second["fetched_at"] == "2026-10-08T23:50:00+08:00"  # 取数时间是缓存里那份的


async def test_forecast_is_cached_but_cities_are_not():
    # 和风不许缓存地理信息（常见问题“我能否缓存数据”），城市每次都查
    clock = [0.0]
    fake = FakeQWeather()
    weather = forecaster(fake, clock=lambda: clock[0])
    await weather.forecast("杭州")
    await weather.forecast("杭州", date(2026, 10, 9))
    await weather.forecast("杭州", date(2026, 10, 20))
    assert fake.paths() == ["lookup", "15d", "168h", "lookup", "lookup"]
    clock[0] += FORECAST_CACHE_SECONDS
    await weather.forecast("杭州")
    assert fake.paths()[5:] == ["lookup", "15d", "168h"]


async def test_lookup_uses_last_segment_and_english_names():
    xihu_hz, xihu_nc = city("西湖", "杭州", "浙江省"), city("西湖", "南昌", "江西省", lid="nc")
    fake = FakeQWeather(cities=[xihu_hz, xihu_nc])
    result = await forecaster(fake).forecast("江西省·南昌·西湖")
    assert result["resolved_location"] == "江西省·南昌·西湖"
    assert fake.requests[0].url.params["location"] == "西湖"

    paris = city("Paris", "Paris", "Ile-de-France", country="France", rank=20, tz="Europe/Paris")
    fake = FakeQWeather(cities=[paris])
    await forecaster(fake).forecast("Paris")
    assert fake.requests[0].url.params["lang"] == "en"


async def test_failures_carry_codes():
    fake = FakeQWeather(cities=[city("朝阳", "朝阳", "辽宁省"), city("朝阳", "北京", "北京市")])
    with pytest.raises(ForecastError) as e:
        await forecaster(fake).forecast("朝阳")
    assert e.value.code == "ambiguous_location" and "北京市·北京·朝阳" in e.value.candidates
    assert fake.paths() == ["lookup"]

    for fake, code in (
        (FakeQWeather(cities=[]), "location_not_found"),
        (FakeQWeather(fail={"lookup": 404}), "location_not_found"),  # 和风对认不出的地名回 404
        (FakeQWeather(fail={"lookup": 401}), "upstream_auth_failed"),
        (FakeQWeather(fail={"15d": 429}), "upstream_rate_limited"),
        (FakeQWeather(fail={"15d": 500}), "upstream_unavailable"),
    ):
        with pytest.raises(ForecastError) as e:
            await forecaster(fake).forecast("杭州")
        assert e.value.code == code

    with pytest.raises(ForecastError) as e:
        await Forecaster(lambda: None).forecast("杭州")
    assert e.value.code == "not_configured"


# ---- MCP 工具 ----


async def call(fake: FakeQWeather, **arguments):
    async with Client(build_mcp(forecaster(fake))) as client:
        return await client.call_tool("get_weather", arguments, raise_on_error=False)


async def test_tool_gives_the_page_everything_and_the_model_no_hourly_detail():
    result = await call(FakeQWeather(), location="杭州", requested_date="2026-10-09")
    assert not result.is_error
    assert result.structured_content["ok"] and len(result.structured_content["hourly"]) == 24
    seen = json.loads(result.content[0].text)
    assert "hourly" not in seen and seen["hourly_summary"]["window"].startswith("2026-10-09T00:00")
    assert seen["forecast_days"][0]["date"] == "2026-10-09" and seen["requested_date"] == "2026-10-09"

    # 不在范围里：模型只看到能查哪几天，没有别的日子
    seen = json.loads((await call(FakeQWeather(), location="杭州", requested_date="2026-10-30")).content[0].text)
    assert seen["forecast_days"] == [] and seen["forecast_range"] == {
        "first_date": "2026-10-08",
        "last_date": "2026-10-22",
    }


async def test_tool_failures_tell_the_model_what_to_do():
    fake = FakeQWeather(cities=[city("朝阳", "朝阳", "辽宁省"), city("朝阳", "北京", "北京市")])
    result = await call(fake, location="朝阳")
    assert not result.is_error  # 和 image 一样，失败也读 structuredContent
    assert result.structured_content == {
        "ok": False,
        "error": {
            "code": "ambiguous_location",
            "message": "The place name matches several areas.",
            "retryable": False,
            "candidates": ["辽宁省·朝阳", "北京市·北京·朝阳"],
        },
    }
    assert '["辽宁省·朝阳", "北京市·北京·朝阳"]' in result.content[0].text and "ask the user" in result.content[0].text

    result = await call(FakeQWeather(cities=[]), location="鼓浪屿")
    assert result.structured_content["error"]["code"] == "location_not_found"
    assert "ask for its city" in result.content[0].text

    result = await call(FakeQWeather(), location="114.05,22.54")
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert "not coordinates" in result.content[0].text

    # 服务这边的问题：只说没有可用结果，不让模型把工具状态讲给用户（Fusion 9-19）
    result = await call(FakeQWeather(fail={"15d": 500}), location="杭州")
    assert result.structured_content["error"] == {
        "code": "upstream_unavailable",
        "message": "QWeather returned an error.",
        "retryable": False,
    }
    assert result.content[0].text.startswith("The weather service returned no usable result.")


def test_description_is_product_neutral():
    for product_specific in (
        "search_web",
        "web_search",
        "read_page",
        "url_read",
        "card",
        "get_weather",
        "current location",
        "device",
    ):
        assert product_specific not in DESCRIPTION
    for rule in ("not for whole provinces", "do not add a city the user did not provide", "Omit requested_date",
                 "clearly beyond the window"):
        assert rule in DESCRIPTION


async def test_mcp_is_served_next_to_rest_and_rest_is_unchanged():
    app = create_app(settings(), FakeQWeather().transport())
    headers = {"Accept": "application/json, text/event-stream"}

    def rpc(body: httpx.Response) -> dict:
        text = body.text
        return json.loads(text.split("data: ", 1)[1]) if text.startswith("event") else body.json()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://weather") as http:
            listed = rpc(
                await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
            )
            (tool,) = listed["result"]["tools"]
            assert tool["name"] == "get_weather" and tool["inputSchema"]["required"] == ["location"]
            assert set(tool["inputSchema"]["properties"]) == {"location", "requested_date"}
            assert tool["annotations"]["readOnlyHint"] is True
            # REST /v1/cities 不多出时区字段（线上 Fusion 在用）
            cities = (await http.get("/v1/cities", params={"location": "杭州"})).json()["cities"]
            assert cities == [
                {
                    "location_id": "101210101",
                    "name": "杭州",
                    "adm1": "浙江省",
                    "adm2": "杭州",
                    "country": "中国",
                    "rank": 15,
                }
            ]

    app = create_app(settings(qweather_private_key=""), FakeQWeather().transport())  # 凭证不全
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://weather") as http:
            body = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_weather", "arguments": {"location": "杭州"}},
            }
            called = rpc(await http.post("/mcp", json=body, headers=headers))
            assert called["result"]["structuredContent"]["error"]["code"] == "not_configured"


async def test_client_only_asks_for_forecast_lengths_qweather_has():
    client = QWeatherClient(credentials(), transport=FakeQWeather().transport())
    with pytest.raises(ValueError):
        await client.daily("101210101", days=14)
    with pytest.raises(ValueError):
        await client.hourly("101210101", hours=48)
