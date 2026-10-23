from fastmcp import FastMCP

from app.cache import get_cached, set_cached
from app.config import settings
from app.models import SearchRequest, SearchType
from app.providers.registry import get_provider

mcp = FastMCP("search", instructions="Search the web using configured providers")


@mcp.tool()
async def search(
    query: str,
    type: str = "web",
    count: int = 10,
    freshness: str | None = None,
) -> dict:
    """Search the web using the configured provider."""
    search_type = SearchType(type)
    req = SearchRequest(query=query, type=search_type, count=count, freshness=freshness)
    provider = get_provider()
    provider_name = settings.SEARCH_PROVIDER

    cached = await get_cached(provider_name, req)
    if cached:
        return cached.model_dump()

    result = await provider.search(req)
    result.provider = result.provider or provider_name
    result.requested_provider = provider_name
    result.result_provider = result.provider
    result.provider_chain = [provider_name]
    await set_cached(result.result_provider, req, result)
    return result.model_dump()


@mcp.tool()
async def search_news(query: str, count: int = 10, freshness: str | None = None) -> dict:
    """Search for recent news articles."""
    return await search(query=query, type="news", count=count, freshness=freshness)


@mcp.tool()
async def search_images(query: str, count: int = 10) -> dict:
    """Search for images."""
    return await search(query=query, type="image", count=count)
