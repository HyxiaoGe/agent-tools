import httpx

from app.config import settings
from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType

TAVILY_API_URL = "https://api.tavily.com/search"


class TavilyProvider:
    def __init__(self) -> None:
        self.api_key = settings.TAVILY_API_KEY

    async def search(self, request: SearchRequest) -> SearchResponse:
        # 构造请求参数
        payload: dict = {
            "query": request.query,
            "search_depth": "advanced",
            "max_results": request.count,
            "include_raw_content": False,
            "include_favicon": True,
        }

        # 搜索类型映射
        if request.type == SearchType.NEWS:
            payload["topic"] = "news"
        else:
            payload["topic"] = "general"

        # 时效性映射（Brave freshness → Tavily time_range）
        if request.freshness:
            freshness_map = {
                "pd": "day",  # past day
                "pw": "week",  # past week
                "pm": "month",  # past month
                "py": "year",  # past year
            }
            payload["time_range"] = freshness_map.get(request.freshness, request.freshness)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TAVILY_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = self._parse_results(data)

        return SearchResponse(
            query=request.query,
            type=request.type,
            provider="tavily",
            cached=False,
            results=results,
        )

    def _parse_results(self, data: dict) -> list[SearchResultItem]:
        items: list[SearchResultItem] = []
        for r in data.get("results", []):
            items.append(
                SearchResultItem(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    description=r.get("content", ""),
                    content=r.get("content", ""),
                    favicon=r.get("favicon"),
                    published_at=r.get("published_date"),
                )
            )
        return items
