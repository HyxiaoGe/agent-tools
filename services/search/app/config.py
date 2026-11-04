from pydantic import field_validator
from pydantic_settings import BaseSettings


def parse_daily_limits(raw: str) -> dict[str, int]:
    """解析 `brave=100,deepseek=1000`；未列出的 provider 不设上限，0 表示当日一次都不调。"""
    limits: dict[str, int] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        name = name.strip().lower()
        if not sep or not name:
            raise ValueError(f"PROVIDER_DAILY_LIMITS 条目格式应为 name=次数: {entry!r}")
        try:
            limit = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"PROVIDER_DAILY_LIMITS 次数必须是整数: {entry!r}") from exc
        if limit < 0:
            raise ValueError(f"PROVIDER_DAILY_LIMITS 次数不能为负: {entry!r}")
        limits[name] = limit
    return limits


KNOWN_PROVIDERS = ("brave", "tavily", "firecrawl", "deepseek")


def parse_provider_list(raw: str) -> list[str]:
    """解析 `firecrawl,tavily`：去空格、转小写、去重保序；出现未知 provider 名直接报错。"""
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in KNOWN_PROVIDERS:
            raise ValueError(f"SEARCH_FALLBACK_PROVIDERS 含未知 provider: {name!r}，可选 {', '.join(KNOWN_PROVIDERS)}")
        if name not in names:
            names.append(name)
    return names


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    # Provider
    SEARCH_PROVIDER: str = "brave"
    # 备用链：主 provider 报错或结果不足时按顺序尝试，够数即停；自动跳过主 provider 和未配置 key 的。
    # 留空时沿用旧行为：注册顺序里第一个非主 provider。
    SEARCH_FALLBACK_PROVIDERS: str = ""
    BRAVE_API_KEY: str = ""
    TAVILY_API_KEY: str = ""
    FIRECRAWL_API_KEY: str = ""
    FIRECRAWL_API_URL: str = "https://api.firecrawl.dev"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_API_URL: str = "https://api.deepseek.com"

    # 一次搜索（主 provider 加备用链）最多用这么久：网关转发 search 限 30 秒，各家超时串行累加会超过它
    SEARCH_DEADLINE_SECONDS: float = 25.0

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Rate limits
    RATE_LIMIT_GLOBAL: str = "40/second"
    RATE_LIMIT_PER_IP: str = "10/second"

    # Cache TTL (seconds)
    CACHE_TTL_WEB: int = 600
    CACHE_TTL_NEWS: int = 300
    CACHE_TTL_IMAGE: int = 1800
    FIRECRAWL_USAGE_CACHE_TTL: int = 300
    FIRECRAWL_HISTORICAL_USAGE_CACHE_TTL: int = 3600
    FIRECRAWL_USAGE_STALE_TTL: int = 3600
    FIRECRAWL_HISTORICAL_USAGE_STALE_TTL: int = 86400

    # 付费 provider 每日调用上限（北京时间自然日）：每次真正请求上游前占一个名额，超额直接跳过该 provider。
    # 缓存命中不占名额。批量实验曾把 Firecrawl 打到 429、再静默转 Brave 烧掉约 $30，这里兜底。
    PROVIDER_DAILY_LIMITS: str = "brave=100,tavily=100,firecrawl=500,deepseek=1000"
    # 用量达到上限的这个比例时先发一次预警；达到上限时再发一次。每个 provider 每天各一次。
    BUDGET_ALERT_RATIO: float = 0.8
    # 飞书自定义机器人（巡检群），留空则只写日志不推送。
    BUDGET_ALERT_WEBHOOK: str = ""
    BUDGET_ALERT_SIGN_SECRET: str = ""

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "info"

    @field_validator("PROVIDER_DAILY_LIMITS")
    @classmethod
    def _validate_daily_limits(cls, value: str) -> str:
        # 配错就让服务起不来，比静默不限额安全。
        parse_daily_limits(value)
        return value

    @field_validator("SEARCH_FALLBACK_PROVIDERS")
    @classmethod
    def _validate_fallback_providers(cls, value: str) -> str:
        # 拼错 provider 名会让备用链悄悄变短，启动时就拦下。
        parse_provider_list(value)
        return value


settings = Settings()
