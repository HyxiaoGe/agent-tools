from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


# 现有的基础服务：产品没写 services 时默认只放行这几个；之后接入的付费服务（地图、天气等）
# 不在默认列表里，必须在 PRODUCTS 里逐个产品写明才放行。
DEFAULT_PRODUCT_SERVICES = ("image", "search", "reader", "trends")


def parse_daily_limits(raw: str) -> dict[str, int]:
    """解析 `search=2000,image=200`；未列出的服务不限，0 表示当天一律拒绝。"""
    limits: dict[str, int] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        name = name.strip().lower()
        if not sep or not name:
            raise ValueError(f"SERVICE_DAILY_LIMITS 条目格式应为 服务=次数: {entry!r}")
        try:
            limit = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"SERVICE_DAILY_LIMITS 次数必须是整数: {entry!r}") from exc
        if limit < 0:
            raise ValueError(f"SERVICE_DAILY_LIMITS 次数不能为负: {entry!r}")
        limits[name] = limit
    return limits


class ProductConfig(BaseModel):
    # 只引用环境变量名，密钥本身不进配置结构，避免出现在日志或报错里。
    service_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    # 该产品允许调用的服务；省略时为 DEFAULT_PRODUCT_SERVICES。
    services: list[str] | None = None
    # 该产品对某些服务的每日上限（北京时间自然日），和 SERVICE_DAILY_LIMITS 的服务总上限同时生效：
    # 几个产品共用一份付费额度时，防止一个产品把全天的额度用光。没写的服务只受总上限。
    daily_limits: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


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
    # 高德官方 MCP 的代理（注入密钥、只读白名单），付费服务，不在产品默认放行列表里。
    amap_upstream: str = "http://amap-mcp:3334"
    # Context7 官方 MCP 的代理（注入密钥、文档工具白名单），密钥按账号限流，不在产品默认放行列表里。
    context7_upstream: str = "http://context7-mcp:3335"
    # 和风天气的只读代理（持有凭证签 JWT、字段归一），按项目计费，不在产品默认放行列表里。
    weather_upstream: str = "http://weather-service:8092"
    flyai_upstream: str = "http://flyai-adapter:8093"
    # 腾讯地图官方 MCP 的代理（注入密钥、只读白名单），按 key 计每日配额，不在产品默认放行列表里。
    tmap_upstream: str = "http://tmap-mcp:3336"
    # 地图组合层：直接调高德、腾讯的 Web 服务，先高德后腾讯，给产品两个 MCP 工具；付费服务，不在产品默认放行列表里。
    maps_upstream: str = "http://maps-service:8094"

    # 超时要大于调用方自己的超时，由调用方决定放弃；image-service 内部总预算 135s。
    image_timeout_seconds: float = 180
    search_timeout_seconds: float = 30
    reader_timeout_seconds: float = 30
    trends_timeout_seconds: float = 60
    amap_timeout_seconds: float = 60
    # Context7 上游常见 5–20s，代理自身 45s 放弃，这里再留余量。
    context7_timeout_seconds: float = 90
    # 代理单次上游 8s，一次工具调用最多两次串行请求，再留余量。
    weather_timeout_seconds: float = 30
    # FlyAI 适配器自身给供应商 CLI 20 秒，再留排队与传输余量。
    flyai_timeout_seconds: float = 30
    tmap_timeout_seconds: float = 60
    # 服务自身一次工具调用最多 25 秒，再留余量。
    maps_timeout_seconds: float = 40

    # 按服务分开的连接池：某个服务堆积慢请求时只占满自己的池，不拖住其他服务。
    image_max_connections: int = 16
    search_max_connections: int = 32
    reader_max_connections: int = 32
    trends_max_connections: int = 16
    amap_max_connections: int = 16
    context7_max_connections: int = 16
    weather_max_connections: int = 16
    flyai_max_connections: int = 8
    tmap_max_connections: int = 16
    maps_max_connections: int = 16
    # 池满时排队等待的上限，超过即返回 503。
    pool_wait_seconds: float = 5

    max_request_body_bytes: int = 10 * 1024 * 1024

    usage_db_path: str = "storage/usage.sqlite3"
    usage_retention_days: int = 90

    # 按服务的每日调用上限（北京时间自然日）：兜底防批量实验或失控循环打穿付费上游，超出返回 429。
    # 只算 REST 请求和 MCP tools/call，不算握手；未列出的服务（免费的 trends、context7）不限。
    # 默认值约为 10-23 ~ 11-01 单日峰值的 5~25 倍；服务内部另有按上游的额度。
    service_daily_limits: str = "search=2000,reader=1000,image=200,weather=1000,amap=1000,tmap=1000,maps=300,flyai=100"
    # 用到上限的这个比例时先推一次预警，用满时再推一次；每个服务每天各一次。
    limit_alert_ratio: float = Field(default=0.8, gt=0, lt=1)
    # 飞书巡检群自定义机器人；留空则只写日志。
    limit_alert_webhook: SecretStr = SecretStr("")
    limit_alert_sign_secret: SecretStr = SecretStr("")

    # 每日用量日报：发到飞书「AI 成本&额度」群的自定义机器人，webhook 留空则不发。
    # webhook 地址本身就是密钥，用 SecretStr 防止随配置对象出现在日志或报错里。
    daily_report_webhook: SecretStr = SecretStr("")
    daily_report_sign_secret: SecretStr = SecretStr("")
    # 北京时间每天几点后发送前一天的日报
    daily_report_hour: int = Field(default=9, ge=0, le=23)
    # 记录哪天已发送，防止重启后重复发
    daily_report_state_path: str = "storage/daily_report_state.json"

    @model_validator(mode="after")
    def _validate_products(self) -> Settings:
        for app_id in self.products:
            if not _APP_ID_PATTERN.fullmatch(app_id):
                raise ValueError("PRODUCTS contains an invalid application identifier")
        key_envs = [product.service_key_env for product in self.products.values()]
        if len(key_envs) != len(set(key_envs)):
            raise ValueError("PRODUCTS must not share a service key")
        known = set(self.upstreams())
        # 配错就让网关起不来，比静默不限额安全。
        unknown_limits = set(parse_daily_limits(self.service_daily_limits)) - known
        if unknown_limits:
            raise ValueError(f"SERVICE_DAILY_LIMITS references unknown services: {sorted(unknown_limits)}")
        for app_id, product in self.products.items():
            unknown = set(product.services or ()) - known
            if unknown:
                raise ValueError(f"PRODUCTS[{app_id}].services references unknown services: {sorted(unknown)}")
            # 给没放行的服务写上限多半是写错了服务名或产品，起不来比静默无效好查。
            not_allowed = set(product.daily_limits) - self.allowed_services(app_id)
            if not_allowed:
                raise ValueError(
                    f"PRODUCTS[{app_id}].daily_limits references services it cannot call: {sorted(not_allowed)}"
                )
        return self

    def daily_limits(self) -> dict[str, int]:
        return parse_daily_limits(self.service_daily_limits)

    def product_daily_limits(self) -> dict[str, dict[str, int]]:
        return {app_id: dict(p.daily_limits) for app_id, p in self.products.items() if p.daily_limits}

    def allowed_services(self, app_id: str) -> frozenset[str]:
        product = self.products[app_id]
        return frozenset(DEFAULT_PRODUCT_SERVICES if product.services is None else product.services)

    def upstreams(self) -> dict[str, Upstream]:
        s = self
        return {
            "image": Upstream("image", s.image_upstream, s.image_timeout_seconds, s.image_max_connections),
            "search": Upstream("search", s.search_upstream, s.search_timeout_seconds, s.search_max_connections),
            "reader": Upstream("reader", s.reader_upstream, s.reader_timeout_seconds, s.reader_max_connections),
            "trends": Upstream("trends", s.trends_upstream, s.trends_timeout_seconds, s.trends_max_connections),
            "amap": Upstream("amap", s.amap_upstream, s.amap_timeout_seconds, s.amap_max_connections),
            "context7": Upstream(
                "context7", s.context7_upstream, s.context7_timeout_seconds, s.context7_max_connections
            ),
            "weather": Upstream("weather", s.weather_upstream, s.weather_timeout_seconds, s.weather_max_connections),
            "flyai": Upstream("flyai", s.flyai_upstream, s.flyai_timeout_seconds, s.flyai_max_connections),
            "tmap": Upstream("tmap", s.tmap_upstream, s.tmap_timeout_seconds, s.tmap_max_connections),
            "maps": Upstream("maps", s.maps_upstream, s.maps_timeout_seconds, s.maps_max_connections),
        }
