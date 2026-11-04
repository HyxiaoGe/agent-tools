from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 两家 Web 服务的 key（和 amap、tmap 两个代理用的是同一把）。缺哪家就只用另一家；两家都缺时工具回 unavailable。
    amap_api_key: SecretStr = SecretStr("")
    tmap_api_key: SecretStr = SecretStr("")
    # 单次上游请求的时限；不重试，转给另一家就相当于重试。
    upstream_timeout_seconds: float = 8.0
    # 一次工具调用的总时限（照 Fusion）；网关转发限 60 秒。
    call_timeout_seconds: float = 25.0
    # 每家同时最多几个请求：个人 key 的每秒上限低。
    provider_concurrency: int = 3
    # 每天打了两家多少次的计数，重启后接着算；不配就只记在内存里。
    usage_state_path: str = ""

    port: int = 8094
