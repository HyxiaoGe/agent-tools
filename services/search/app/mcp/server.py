"""搜索服务的 MCP（/mcp，无状态，和 weather 一样）。

- search_web：给产品的模型用的工具，见 app.mcp.search_web。
- search、search_news、search_images：早先的原样透传工具，返回整份 SearchResponse。现在也走 app.searching
  （有备用链，不再把 0 条、放宽时效的结果写进和 REST 共用的缓存）。
"""

from fastmcp import FastMCP

from app.mcp import search_web
from app.models import SearchRequest, SearchType
from app.searching import SearchFailed, run_search
from app.usage import record_search_credits

mcp = FastMCP(
    "search",
    instructions="search_web searches the web and returns candidate sources; see the tool description.",
)
search_web.register(mcp)


@mcp.tool()
async def search(
    query: str,
    type: str = "web",
    count: int = 10,
    freshness: str | None = None,
) -> dict:
    """Search the web using the configured provider."""
    req = SearchRequest(query=query, type=SearchType(type), count=count, freshness=freshness)
    try:
        outcome = await run_search(req)
    except SearchFailed as e:
        # 抛回主 provider 的原异常（状态码照旧）；from None：它和 SearchFailed 互为 cause 会成环
        raise e.primary from None
    for provider, credits_used in outcome.credits:
        await record_search_credits(provider, credits_used)
    return outcome.response.model_dump()


@mcp.tool()
async def search_news(query: str, count: int = 10, freshness: str | None = None) -> dict:
    """Search for recent news articles."""
    return await search(query=query, type="news", count=count, freshness=freshness)


@mcp.tool()
async def search_images(query: str, count: int = 10) -> dict:
    """Search for images."""
    return await search(query=query, type="image", count=count)
