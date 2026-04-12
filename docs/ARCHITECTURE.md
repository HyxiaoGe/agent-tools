# Architecture

## Layer diagram

```
                        +------------------+
  HTTP clients -------->|   FastAPI app    |
                        |   (src/main.py)  |
                        +--------+---------+
                                 |
              +------------------+------------------+
              |                  |                   |
     +--------v-------+  +------v------+   +--------v--------+
     |  src/api/       |  | src/mcp/    |   | /metrics        |
     |  REST endpoints |  | MCP tools   |   | Prometheus      |
     +--------+--------+  +------+------+   +-----------------+
              |                  |
     +--------v------------------v--------+
     |         src/middleware/             |
     |   auth (Bearer) + rate_limit       |
     +--------+---------------------------+
              |
     +--------v--------+
     |  src/services/   |
     |  generator.py    |------> Google Gemini API
     |  cache.py        |------> Redis
     |  metrics.py      |------> Prometheus counters
     +------------------+
              |
     +--------v--------+
     |  src/models/     |
     |  request.py      |
     |  response.py     |
     +-----------------+
```

## Request flow (POST /v1/generate)

1. `api/generate.py` receives request, validates via Pydantic model
2. `middleware/auth.py` checks Bearer token (if API_KEY configured)
3. `middleware/rate_limit.py` checks global + per-IP counters in Redis
4. `services/generator.py` orchestrates:
   - Compute cache key (SHA-256 of normalized params)
   - Check Redis cache -> return cached result if hit
   - Acquire Redis lock (thundering herd protection)
   - Call Gemini API with retries (max 2 retries, 300s timeout)
   - Save image to disk (`storage/images/`)
   - Store metadata in Redis cache (7-day TTL)
   - Release lock
5. Response includes image URL served via FastAPI StaticFiles

## MCP interface

Mounted at `/mcp` (stateless HTTP). Two tools:
- `generate_image` -- same flow as REST, returns text summary with full URL
- `list_models` -- returns available model list

## Layer dependency rules

- `api/` and `mcp/` may import from `services/`, `models/`, `middleware/`, `config`
- `middleware/` may import from `models/`, `config`
- `services/` may import from `models/`, `config`
- `models/` is leaf -- no imports from other src packages
- `config.py` is leaf -- no imports from other src packages
