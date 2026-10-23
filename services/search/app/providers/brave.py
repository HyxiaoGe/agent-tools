import httpx

from app.config import settings
from app.models import SearchRequest, SearchResponse, SearchResultItem, SearchType

BRAVE_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
BRAVE_IMAGE_URL = "https://api.search.brave.com/res/v1/images/search"


class BraveProvider:
    def __init__(self) -> None:
        self.api_key = settings.BRAVE_API_KEY

    def _url_for_type(self, search_type: SearchType) -> str:
        return {
            SearchType.WEB: BRAVE_WEB_URL,
            SearchType.NEWS: BRAVE_NEWS_URL,
            SearchType.IMAGE: BRAVE_IMAGE_URL,
        }[search_type]

    async def search(self, request: SearchRequest) -> SearchResponse:
        url = self._url_for_type(request.type)
        params: dict = {
            "q": request.query,
            "count": request.count,
            "offset": (request.page - 1) * request.count,
            "search_lang": request.lang,
            "country": request.region,
        }
        if request.freshness:
            params["freshness"] = request.freshness

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = self._parse_results(data, request.type)

        return SearchResponse(
            query=request.query,
            type=request.type,
            provider="brave",
            cached=False,
            results=results,
        )

    def _parse_results(self, data: dict, search_type: SearchType) -> list[SearchResultItem]:
        items: list[SearchResultItem] = []

        if search_type == SearchType.WEB:
            for r in data.get("web", {}).get("results", []):
                items.append(
                    SearchResultItem(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        description=r.get("description", ""),
                        published_at=r.get("page_age"),
                    )
                )
        elif search_type == SearchType.NEWS:
            for r in data.get("results", []):
                items.append(
                    SearchResultItem(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        description=r.get("description", ""),
                        published_at=r.get("age"),
                    )
                )
        elif search_type == SearchType.IMAGE:
            for r in data.get("results", []):
                items.append(
                    SearchResultItem(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        description=r.get("source", ""),
                    )
                )

        return items
