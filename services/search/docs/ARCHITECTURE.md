# Architecture

## Overview

Search Service is a FastAPI middleware that provides a unified search API over multiple providers. It adds caching, rate limiting, fallback logic, and MCP tool exposure on top of raw provider APIs.

## Layers

### 1. Config (`app/config.py`)
- Single `Settings` class using pydantic-settings
- Reads from `.env`, no hardcoded secrets
- Imported by all other layers

### 2. Models (`app/models.py`)
- `SearchRequest` / `SearchResponse` / `SearchResultItem` -- shared across all layers
- `SearchType` enum: web, news, image
- No business logic, pure data shapes

### 3. Providers (`app/providers/`)
- `base.py` -- `SearchProvider` Protocol defining the contract
- `brave.py` / `tavily.py` / `firecrawl.py` / `deepseek.py` -- concrete implementations using httpx
- `registry.py` -- lazy init, lookup by name, fallback selection
- Each provider parses its own API response into `SearchResultItem`

### 4. Infrastructure (`app/cache.py`, `app/limiter.py`, `app/logger.py`)
- `cache.py` -- async Redis client, SHA-256 cache keys, TTL per search type
- `limiter.py` -- slowapi rate limiter (per-IP)
- `logger.py` -- structlog with JSON output (console in debug mode)

### 5. Search flow (`app/searching.py`)
- `run_search()` -- cache check -> primary provider -> fallback chain -> cache set; shared by REST and MCP
- Total deadline `SEARCH_DEADLINE_SECONDS` (25s, under the gateway's 30s) across primary + fallbacks
- Cache is keyed by the requested provider for both read and write, so fallback results are reused
- `restrict_domains=True` (MCP) filters results to `domain_filters`, because Brave and Tavily ignore them
- Raises `SearchFailed(primary, errors)` when nothing succeeded; REST re-raises `primary` (503 / 500 as before)

### 6. Routes (`app/routes/`)
- `search.py` -- POST /search: thin wrapper over `run_search()`, Firecrawl credits recorded in a background task
- `admin.py` -- health check, provider listing, cache flush

### 7. MCP (`app/mcp/`)
- Stateless streamable HTTP at `/mcp` (one POST per call, no session)
- `search_web.py` -- `search_web`, the tool products give their models: validated arguments, structured errors
  `{code, message, retryable}`, deduplicated and trimmed results, English description shared by all products
- `server.py` -- the earlier pass-through tools `search` / `search_news` / `search_images` (full `SearchResponse`)
- All tools go through `run_search()` (no route dependency)

### 8. App Entry (`app/main.py`)
- Lifespan: setup logging, MCP lifespan, Redis cleanup
- Mounts routers and MCP app

## Dependency Rules

```
config, models  <-- imported by everything (foundation)
providers       <-- depends on config, models only
cache, limiter  <-- depends on config, models only
searching       <-- depends on providers (registry), cache, budget, models, config (NOT routes, NOT mcp)
routes          <-- depends on searching, limiter, logger, models, config
mcp             <-- depends on searching, budget, models (NOT routes, NOT concrete providers)
main            <-- wires everything together
```

## Data Flow

```
Client -> POST /search (or MCP tools/call)
  -> rate limit check (REST only)
  -> Redis cache lookup (Redis errors: log and treat as a miss)
  -> if miss: provider.search()
  -> if error / insufficient results: fallback chain (SEARCH_FALLBACK_PROVIDERS, in order, stop when enough,
     stop at the total deadline)
  -> cache result under the requested provider (if quality threshold met and time filter not relaxed)
  -> return SearchResponse
```
