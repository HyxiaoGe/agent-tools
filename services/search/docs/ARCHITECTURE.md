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

### 5. Routes (`app/routes/`)
- `search.py` -- POST /search: cache check -> provider call -> fallback -> cache set
- `admin.py` -- health check, provider listing, cache flush

### 6. MCP (`app/mcp/`)
- FastMCP tools wrapping provider + cache directly (no route dependency)
- Mounted at root path in main.py

### 7. App Entry (`app/main.py`)
- Lifespan: setup logging, MCP lifespan, Redis cleanup
- Mounts routers and MCP app

## Dependency Rules

```
config, models  <-- imported by everything (foundation)
providers       <-- depends on config, models only
cache, limiter  <-- depends on config, models only
routes          <-- depends on providers, cache, limiter, logger, models, config
mcp             <-- depends on providers, cache, models (NOT routes)
main            <-- wires everything together
```

## Data Flow

```
Client -> POST /search
  -> rate limit check
  -> Redis cache lookup
  -> if miss: provider.search()
  -> if insufficient results: fallback provider
  -> cache result (if quality threshold met)
  -> return SearchResponse
```
