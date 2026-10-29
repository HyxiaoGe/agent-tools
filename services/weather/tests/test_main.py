import base64
import json
import logging

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app.config import Settings
from app.main import HTTP_CLIENT_LOGGERS, _configure_logging, build_client, create_app
from app.qweather import (
    TOKEN_REFRESH_MARGIN_SECONDS,
    TOKEN_TTL_SECONDS,
    QWeatherClient,
    QWeatherCredentials,
    QWeatherError,
    sign_jwt,
)

pytestmark = pytest.mark.anyio

PRIVATE_KEY = Ed25519PrivateKey.generate()
PEM = PRIVATE_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
SINGLE_LINE = "".join(line for line in PEM.splitlines() if not line.startswith("-----"))
HOST = "abc.re.qweatherapi.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(**overrides) -> Settings:
    values = dict(
        qweather_api_host=HOST,
        qweather_key_id="KID",
        qweather_project_id="PROJ",
        qweather_developer_id="DEV",
        qweather_private_key=SINGLE_LINE,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def credentials(**overrides) -> QWeatherCredentials:
    return QWeatherCredentials.from_settings(
        api_host=HOST, key_id="KID", project_id="PROJ", developer_id="DEV", private_key=SINGLE_LINE, **overrides
    )


def b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


CITY = {"id": "101210101", "name": "杭州", "adm1": "浙江省", "adm2": "杭州", "country": "中国", "rank": "15"}
DAY = {
    "fxDate": "2026-10-08",
    "textDay": "晴",
    "textNight": "多云",
    "tempMax": "26",
    "tempMin": "17",
    "windDirDay": "东风",
    "windDirNight": "北风",
    "windScaleDay": "1-3",
    "windScaleNight": "1-3",
    "humidity": "60",
}
HOUR = {"fxTime": "2026-10-08T17:00+08:00", "temp": "20", "text": "多云", "pop": "10", "precip": "0.0"}


class FakeQWeather:
    """模拟和风 API：记录请求，按路径回固定数据，可指定状态码。"""

    def __init__(self, *, status: int = 200, body: dict | None = None, raise_timeout: bool = False) -> None:
        self.status = status
        self.body = body
        self.raise_timeout = raise_timeout
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raise_timeout:
                raise httpx.ReadTimeout("slow", request=request)
            if self.body is not None or self.status != 200:
                return httpx.Response(
                    self.status, json=self.body if self.body is not None else {"code": str(self.status)}
                )
            if request.url.path == "/geo/v2/city/lookup":
                return httpx.Response(200, json={"code": "200", "location": [CITY, {"name": "缺 id"}]})
            if request.url.path == "/v7/weather/7d":
                return httpx.Response(200, json={"code": "200", "daily": [DAY, {"fxDate": "2026-10-09"}]})
            if request.url.path == "/v7/weather/24h":
                return httpx.Response(200, json={"code": "200", "hourly": [HOUR, {"fxTime": "x"}]})
            return httpx.Response(404)

        return httpx.MockTransport(handle)


async def api(fake: FakeQWeather | None = None, config: Settings | None = None):
    app = create_app(config or settings(), (fake or FakeQWeather()).transport())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://weather") as client:
            yield client


class TestCredentials:
    def test_incomplete_settings_disable_and_invalid_values_raise(self):
        assert build_client(settings(qweather_key_id="")) is None
        assert build_client(settings(qweather_private_key="  ")) is None
        with pytest.raises(ValueError, match="QWEATHER_PRIVATE_KEY"):
            build_client(settings(qweather_private_key="not-a-key"))
        with pytest.raises(ValueError, match="QWEATHER_API_HOST"):
            build_client(settings(qweather_api_host="https://evil.example.com/path"))
        with pytest.raises(ValueError, match="QWEATHER_API_HOST"):
            build_client(settings(qweather_api_host="api.example.com"))
        # 完整 PEM 与单行 base64 都接受。
        assert build_client(settings(qweather_private_key=PEM)) is not None

    def test_jwt_has_only_documented_fields_and_valid_signature(self):
        token = sign_jwt(credentials(), issued_at=1_700_000_000)
        header, payload, signature = token.split(".")
        assert json.loads(b64decode(header)) == {"alg": "EdDSA", "kid": "KID"}
        assert json.loads(b64decode(payload)) == {
            "iss": "DEV",
            "sub": "PROJ",
            "iat": 1_700_000_000,
            "exp": 1_700_000_000 + TOKEN_TTL_SECONDS,
        }
        PRIVATE_KEY.public_key().verify(b64decode(signature), f"{header}.{payload}".encode())


class TestClient:
    async def test_token_is_reused_until_refresh_margin(self):
        now = {"value": 1_700_000_000.0}
        fake = FakeQWeather()
        client = QWeatherClient(credentials(), transport=fake.transport(), clock=lambda: now["value"])
        await client.lookup_city("杭州")
        first = fake.requests[-1].headers["Authorization"]
        now["value"] += TOKEN_TTL_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS - 60
        await client.daily_forecast("101210101")
        assert fake.requests[-1].headers["Authorization"] == first
        now["value"] += 120
        await client.daily_forecast("101210101")
        assert fake.requests[-1].headers["Authorization"] != first
        assert first.startswith("Bearer ")
        assert fake.requests[0].url.host == HOST
        await client.aclose()

    async def test_http_errors_map_to_codes(self):
        cases = {
            401: "upstream_auth_failed",
            403: "upstream_auth_failed",
            429: "upstream_rate_limited",
            500: "upstream_unavailable",
        }
        for status, code in cases.items():
            client = QWeatherClient(credentials(), transport=FakeQWeather(status=status).transport())
            with pytest.raises(QWeatherError) as error:
                await client.daily_forecast("101210101")
            assert error.value.code == code
        client = QWeatherClient(credentials(), transport=FakeQWeather(raise_timeout=True).transport())
        with pytest.raises(QWeatherError, match="upstream_timeout"):
            await client.daily_forecast("101210101")
        client = QWeatherClient(credentials(), transport=FakeQWeather(body={"code": "402"}).transport())
        with pytest.raises(QWeatherError, match="invalid_response"):
            await client.daily_forecast("101210101")
        # 城市查询查无结果：HTTP 404 / 业务码 404 都当空列表，不当错误。
        assert (
            await QWeatherClient(credentials(), transport=FakeQWeather(status=404).transport()).lookup_city("x") == []
        )
        assert (
            await QWeatherClient(credentials(), transport=FakeQWeather(body={"code": "404"}).transport()).lookup_city(
                "x"
            )
            == []
        )


class TestApi:
    async def test_health_reports_configuration(self):
        async for client in api():
            response = await client.get("/health")
            assert response.json() == {"status": "ok", "configured": True}
        async for client in api(config=settings(qweather_key_id="")):
            assert (await client.get("/health")).json()["configured"] is False
            response = await client.get("/v1/cities", params={"location": "杭州"})
            assert response.status_code == 503
            assert response.json() == {"error": {"code": "weather_not_configured"}}

    async def test_cities_are_normalized_and_parameters_forwarded(self):
        fake = FakeQWeather()
        async for client in api(fake):
            response = await client.get("/v1/cities", params={"location": " 杭州 ", "number": 3, "lang": "en"})
        assert response.status_code == 200
        assert response.json() == {
            "cities": [
                {
                    "location_id": "101210101",
                    "name": "杭州",
                    "adm1": "浙江省",
                    "adm2": "杭州",
                    "country": "中国",
                    "rank": 15,
                }
            ]
        }
        request = fake.requests[0]
        assert request.url.path == "/geo/v2/city/lookup"
        assert dict(request.url.params) == {"location": "杭州", "number": "3", "lang": "en"}
        assert request.headers["Authorization"].startswith("Bearer ")

    async def test_forecasts_are_normalized(self):
        async for client in api():
            daily = (await client.get("/v1/forecast/daily", params={"location_id": "101210101"})).json()
            hourly = (await client.get("/v1/forecast/hourly", params={"location_id": "120.15,30.28"})).json()
        assert daily == {
            "daily": [
                {
                    "date": "2026-10-08",
                    "text_day": "晴",
                    "text_night": "多云",
                    "temp_max": "26",
                    "temp_min": "17",
                    "wind_dir_day": "东风",
                    "wind_dir_night": "北风",
                    "wind_scale_day": "1-3",
                    "wind_scale_night": "1-3",
                }
            ]
        }
        assert hourly == {
            "hourly": [{"time": "2026-10-08T17:00+08:00", "temp": "20", "text": "多云", "pop": "10", "precip": "0.0"}]
        }

    async def test_invalid_parameters_are_rejected_before_calling_upstream(self):
        fake = FakeQWeather()
        async for client in api(fake):
            assert (await client.get("/v1/cities")).status_code == 422
            assert (await client.get("/v1/cities", params={"location": "x" * 121})).status_code == 422
            assert (await client.get("/v1/cities", params={"location": "杭州", "number": 0})).status_code == 422
            assert (await client.get("/v1/cities", params={"location": "杭州", "lang": "fr"})).status_code == 422
            assert (await client.get("/v1/forecast/daily", params={"location_id": "../geo"})).status_code == 422
            assert (await client.get("/v1/forecast/hourly", params={"location_id": "a b"})).status_code == 422
        assert fake.requests == []

    async def test_upstream_failures_become_structured_errors(self):
        for status, expected_status, code in (
            (401, 502, "upstream_auth_failed"),
            (429, 429, "upstream_rate_limited"),
            (500, 502, "upstream_unavailable"),
        ):
            async for client in api(FakeQWeather(status=status)):
                response = await client.get("/v1/forecast/daily", params={"location_id": "101210101"})
            assert (response.status_code, response.json()) == (expected_status, {"error": {"code": code}})
        async for client in api(FakeQWeather(raise_timeout=True)):
            response = await client.get("/v1/forecast/daily", params={"location_id": "101210101"})
        assert (response.status_code, response.json()) == (504, {"error": {"code": "upstream_timeout"}})

    def test_http_client_logs_are_silenced(self):
        _configure_logging()
        for name in HTTP_CLIENT_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
