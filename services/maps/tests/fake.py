"""假的高德、腾讯：按“哪家 + 路径 + 部分参数”回录好的样本，并记下每次请求。
样本在 tests/fixtures，11-04 用真 key 录的，去掉了路线折线。

没登记的请求直接让测试失败，免得悄悄回个默认值把问题盖住。key 只核对、不记进 calls。
"""

from __future__ import annotations

import asyncio
import json
from functools import cache
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
HOSTS = {"restapi.amap.com": "amap", "apis.map.qq.com": "tmap"}
KEYS = {"amap": "amap-test-key-0123456789", "tmap": "TMAPTEST-KEY01-23456-78901-ABCDE"}

AMAP_QUOTA = {"status": "0", "info": "USER_DAILY_QUERY_OVER_LIMIT", "infocode": "10044"}
AMAP_EMPTY = {"status": "1", "count": "0", "pois": []}
TMAP_QUOTA = {"status": 121, "message": "此key每日调用量已达到上限", "request_id": "x"}


@cache
def _load(provider: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{provider}.json").read_text())


def sample(provider: str, name: str) -> dict[str, Any]:
    return _load(provider)[name]


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"amap_api_key": KEYS["amap"], "tmap_api_key": KEYS["tmap"], "usage_state_path": ""}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Delay:
    def __init__(self, seconds: float, response: Any):
        self.seconds = seconds
        self.response = response


class FakeUpstream:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, dict[str, str], Any]] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def on(self, provider: str, path: str, response: Any, **match: Any) -> FakeUpstream:
        """response：dict 是 200 的 JSON；int 是 HTTP 状态码；异常照样抛；Delay 先等再回；可调用的收到 request。
        后登记的先匹配。"""
        self.routes.append((provider, path, {k: str(v) for k, v in match.items()}, response))
        return self

    def sample(self, provider: str, name: str, **match: Any) -> FakeUpstream:
        item = sample(provider, name)
        return self.on(provider, item["path"], item["body"], **match)

    def paths(self, provider: str | None = None) -> list[str]:
        return [path for p, path, _ in self.calls if provider is None or p == provider]

    def params(self, provider: str, path: str) -> list[dict[str, str]]:
        return [params for p, called, params in self.calls if p == provider and called == path]

    async def _respond(self, request: httpx.Request, response: Any) -> httpx.Response:
        if isinstance(response, Delay):
            await asyncio.sleep(response.seconds)
            response = response.response
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, int):
            return httpx.Response(response, text="error")
        if isinstance(response, str):
            return httpx.Response(200, text=response)
        if callable(response):
            return response(request)
        return httpx.Response(200, json=response)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        provider = HOSTS[request.url.host]
        params = dict(request.url.params)
        assert params.pop("key") == KEYS[provider]
        self.calls.append((provider, request.url.path, params))
        for p, path, match, response in reversed(self.routes):
            if p == provider and path == request.url.path and all(params.get(k) == v for k, v in match.items()):
                return await self._respond(request, response)
        raise AssertionError(f"unexpected request: {provider} {request.url.path} {params}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())
