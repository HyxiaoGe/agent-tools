from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class ProductConfig(BaseModel):
    # 只引用环境变量名，密钥本身不进配置结构，避免出现在日志或报错里。
    service_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    timeout_seconds: float
    max_connections: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    products: dict[str, ProductConfig] = Field(default_factory=dict)

    # image-service 跑在宿主网络（要用宿主上的代理），其余在专用网络 agent-tools-internal。
    image_upstream: str = "http://host.docker.internal:8090"
    search_upstream: str = "http://search-service:8080"
    reader_upstream: str = "http://reader-service:8091"
    # TrendRadar 官方 MCP 镜像，只读 dev 上 TrendRadar 抓取的热榜数据。
    trends_upstream: str = "http://trends-mcp:3333"

    # 超时要大于调用方自己的超时，由调用方决定放弃；image-service 内部总预算 135s。
    image_timeout_seconds: float = 180
    search_timeout_seconds: float = 30
    reader_timeout_seconds: float = 30
    trends_timeout_seconds: float = 60

    # 按服务分开的连接池：某个服务堆积慢请求时只占满自己的池，不拖住其他服务。
    image_max_connections: int = 16
    search_max_connections: int = 32
    reader_max_connections: int = 32
    trends_max_connections: int = 16
    # 池满时排队等待的上限，超过即返回 503。
    pool_wait_seconds: float = 5

    max_request_body_bytes: int = 10 * 1024 * 1024

    usage_db_path: str = "storage/usage.sqlite3"
    usage_retention_days: int = 90

    @model_validator(mode="after")
    def _validate_products(self) -> Settings:
        for app_id in self.products:
            if not _APP_ID_PATTERN.fullmatch(app_id):
                raise ValueError("PRODUCTS contains an invalid application identifier")
        key_envs = [product.service_key_env for product in self.products.values()]
        if len(key_envs) != len(set(key_envs)):
            raise ValueError("PRODUCTS must not share a service key")
        return self

    def upstreams(self) -> dict[str, Upstream]:
        s = self
        return {
            "image": Upstream("image", s.image_upstream, s.image_timeout_seconds, s.image_max_connections),
            "search": Upstream("search", s.search_upstream, s.search_timeout_seconds, s.search_max_connections),
            "reader": Upstream("reader", s.reader_upstream, s.reader_timeout_seconds, s.reader_max_connections),
            "trends": Upstream("trends", s.trends_upstream, s.trends_timeout_seconds, s.trends_max_connections),
        }
