# Search Service

Universal search middleware -- unified API over multiple search providers (Brave, Tavily) with Redis caching, rate limiting, and MCP tool exposure.

## Architecture

```
app/
  main.py          -- FastAPI app, lifespan, router/MCP mounting
  config.py        -- pydantic-settings (Settings from .env)
  models.py        -- SearchRequest, SearchResponse, SearchType
  cache.py         -- Redis cache (get/set/flush, TTL per search type; get/set errors are logged, never raised)
  searching.py     -- run_search(): cache -> primary -> fallback chain -> cache, total deadline; shared by REST and MCP
  budget.py        -- per-provider daily call budget (Redis counters, Feishu alerts, 503 when exhausted)
  limiter.py       -- slowapi rate limiter
  logger.py        -- structlog setup (JSON in prod, console in debug)
  providers/
    base.py        -- SearchProvider protocol
    registry.py    -- provider init, get/fallback chain/list (every provider wrapped by BudgetGuardedProvider)
    brave.py       -- Brave Search API
    tavily.py      -- Tavily Search API
    firecrawl.py   -- Firecrawl Search API
    deepseek.py    -- DeepSeek Search API
  routes/
    search.py      -- POST /search (thin wrapper over searching.run_search)
    admin.py       -- GET /health, GET /providers, DELETE /cache
  mcp/
    server.py      -- FastMCP app (stateless, /mcp); earlier pass-through tools search, search_news, search_images
    search_web.py  -- search_web: the tool products give their models (validation, trimmed results, structured errors)
tests/
  test_search.py   -- model / provider / route unit tests
  test_budget.py   -- daily budget guard
  test_fallback_chain.py -- fallback chain ordering and route behaviour
  test_search_web.py -- search_web arguments / results / errors, domain filtering, deadline, cache failures
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed layer descriptions.
See [docs/CODING_CONVENTIONS.md](docs/CODING_CONVENTIONS.md) for code style rules.

## Key Constraints

- Providers must implement the `SearchProvider` protocol (app/providers/base.py)
- Routes must not import providers directly -- use registry
- MCP tools must not import routes -- call `app.searching.run_search` (same fallback, cache and budget as REST)
- The search flow (`app/searching.py`) must not import routes or mcp
- `search_web`'s description is read by Fusion's and LingDraft's models: English, no product-specific strategy,
  no other tool names, no current year
- No cross-imports between providers
- Paid upstream calls are capped per day by `PROVIDER_DAILY_LIMITS`; never bypass `registry` (that would skip the budget)

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

Copy `.env.example` to `.env`. Providers are registered only when their API key is set (Brave included); the fallback chain comes from `SEARCH_FALLBACK_PROVIDERS` (tried in order, stops once results are sufficient; skips the primary and providers without a key). If unset, it is the first registered provider other than the primary (registration order brave → tavily → firecrawl → deepseek).
