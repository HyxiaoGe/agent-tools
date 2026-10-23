from enum import StrEnum

from pydantic import BaseModel, Field


class SearchType(StrEnum):
    WEB = "web"
    NEWS = "news"
    IMAGE = "image"


class SearchRequest(BaseModel):
    query: str
    type: SearchType = SearchType.WEB
    provider: str | None = None
    count: int = Field(default=10, ge=1, le=50)
    page: int = Field(default=1, ge=1)
    lang: str = "en"
    region: str = "us"
    freshness: str | None = None
    domain_filters: list[str] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    title: str
    url: str
    description: str
    content: str | None = None  # 网页正文摘要（Tavily 等 provider 支持）
    favicon: str | None = None  # 网站 favicon URL
    published_at: str | None = None


class SearchResponse(BaseModel):
    query: str
    type: SearchType
    provider: str
    requested_provider: str | None = None
    result_provider: str | None = None
    fallback_used: bool = False
    provider_chain: list[str] = Field(default_factory=list)
    relaxed_freshness: bool = False
    credits_used: int | None = None
    cached: bool = False
    cache_key_version: int = 3
    results: list[SearchResultItem] = []


class ProviderUsageResponse(BaseModel):
    provider: str
    available: bool
    remaining_credits: int | None = None
    plan_credits: int | None = None
    used_credits: int | None = None
    usage_ratio: float | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    recorded_usage: "ProviderRecordedUsage | None" = None


class ProviderRecordedUsage(BaseModel):
    provider: str
    available: bool = True
    credits_used: int
    request_count: int
    period_start: str | None = None
    period_end: str | None = None
    source: str = "search_response_credits_used"
    daily: list["ProviderRecordedUsageDay"] = Field(default_factory=list)


class ProviderRecordedUsageDay(BaseModel):
    date: str
    credits_used: int
    request_count: int


class ProviderUsagePeriod(BaseModel):
    start_date: str
    end_date: str
    api_key: str | None = None
    total_credits: int


class ProviderHistoricalUsageResponse(BaseModel):
    provider: str
    available: bool
    by_api_key: bool = False
    periods: list[ProviderUsagePeriod] = Field(default_factory=list)
