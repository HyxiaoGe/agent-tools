from typing import Protocol

from app.models import SearchRequest, SearchResponse


class SearchProvider(Protocol):
    async def search(self, request: SearchRequest) -> SearchResponse: ...
