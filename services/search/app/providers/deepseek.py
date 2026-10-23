from datetime import UTC, datetime, timedelta

import httpx

from app.config import settings
from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType

# Brave 风格 freshness → DeepSeek after: 日期回溯天数
FRESHNESS_DAYS = {
    "pd": 1,
    "pw": 7,
    "pm": 31,
    "py": 365,
}


class DeepSeekProvider:
    """DeepSeek Search API：站点与时间范围用查询语法表达（site: / after:）。"""

    def __init__(self) -> None:
        self.api_key = settings.DEEPSEEK_API_KEY
        self.search_url = f"{settings.DEEPSEEK_API_URL.rstrip('/')}/v1/tools/search"

    async def search(self, request: SearchRequest) -> SearchResponse:
        if request.type == SearchType.IMAGE:
            # 不支持图片搜索；抛错交给路由层走备用 provider
            raise ValueError("deepseek provider does not support image search")

        payload = {
            "query": self._build_query(request),
            "count": request.count,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(self.search_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        return SearchResponse(
            query=request.query,
            type=request.type,
            provider="deepseek",
            cached=False,
            results=self._parse_results(data),
        )

    def _build_query(self, request: SearchRequest) -> str:
        parts = [request.query.strip()]
        if request.domain_filters:
            parts.append(" OR ".join(f"site:{domain}" for domain in request.domain_filters))
        if request.freshness:
            days = FRESHNESS_DAYS.get(request.freshness)
            if days is not None:
                after = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
                parts.append(f"after:{after}")
        return " ".join(parts)

    def _parse_results(self, data: dict) -> list[SearchResultItem]:
        items: list[SearchResultItem] = []
        for r in data.get("results") or []:
            snippet = r.get("snippet") or ""
            items.append(
                SearchResultItem(
                    title=r.get("title") or "",
                    url=r.get("url") or "",
                    description=snippet,
                    # summary 是给模型读的长摘要，作为正文摘要下发
                    content=r.get("summary") or snippet,
                    favicon=r.get("site_icon"),
                    published_at=r.get("date"),
                )
            )
        return items
