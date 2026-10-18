from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    # Provider
    SEARCH_PROVIDER: str = "brave"
    BRAVE_API_KEY: str = ""
    TAVILY_API_KEY: str = ""
    FIRECRAWL_API_KEY: str = ""
    FIRECRAWL_API_URL: str = "https://api.firecrawl.dev"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_API_URL: str = "https://api.deepseek.com"

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

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "info"


settings = Settings()
