# Search Service

Universal search middleware -- unified API over multiple search providers (Brave, Tavily) with Redis caching, rate limiting, and MCP tool exposure.

## Architecture

```
app/
  main.py          -- FastAPI app, lifespan, router/MCP mounting
  config.py        -- pydantic-settings (Settings from .env)
  models.py        -- SearchRequest, SearchResponse, SearchType
  cache.py         -- Redis cache (get/set/flush, TTL per search type)
  limiter.py       -- slowapi rate limiter
  logger.py        -- structlog setup (JSON in prod, console in debug)
  providers/
    base.py        -- SearchProvider protocol
    registry.py    -- provider init, get/fallback/list
    brave.py       -- Brave Search API
    tavily.py      -- Tavily Search API
    firecrawl.py   -- Firecrawl Search API
    deepseek.py    -- DeepSeek Search API
  routes/
    search.py      -- POST /search (with fallback + cache logic)
    admin.py       -- GET /health, GET /providers, DELETE /cache
  mcp/
    server.py      -- FastMCP tools: search, search_news, search_images
tests/
  test_search.py   -- model unit tests
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed layer descriptions.
See [docs/CODING_CONVENTIONS.md](docs/CODING_CONVENTIONS.md) for code style rules.

## Key Constraints

- Providers must implement the `SearchProvider` protocol (app/providers/base.py)
- Routes must not import providers directly -- use registry
- MCP tools must not import routes -- call providers/cache directly
- No cross-imports between providers

## Dev Commands

```bash
uv run uvicorn app.main:app --reload          # dev server
uv run pytest                                  # tests
ruff check --fix . && ruff format .            # lint + format
python scripts/check_architecture.py           # layer dependency check
```

## Deploy

Docker Compose on dev server. CI runs `scripts/check_architecture.py` + ruff before deploy.
Port: 8080. Redis required (see docker-compose.yml).

## Environment

Copy `.env.example` to `.env`. Required: `BRAVE_API_KEY` or `TAVILY_API_KEY`.
