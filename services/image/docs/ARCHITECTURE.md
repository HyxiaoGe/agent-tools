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
     |   auth (product service key)       |
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
2. `middleware/auth.py` authenticates the product service key for REST and HTTP MCP
3. Both entry points pass the verified caller to the shared generation service
4. `services/generator.py` orchestrates:
   - Apply one configurable generation deadline (135s by default); resolve model / quality
   - `services/admission.py` atomically checks global/product/subject request rates and concurrency in Redis; reject before upstream on overload or admission failure
   - Hold a bounded per-request lease through generation/cache waits, then release on success, failure or cancellation
   - Compute cache key (SHA-256 of normalized params)
   - Check Redis cache -> return cached result if hit
   - Acquire Redis lock (thundering herd protection)
   - `services/quota.py` reserves one slot of the provider's daily quota right before each upstream call (`PROVIDER_DAILY_LIMITS`); a used-up provider raises `quota_exceeded`, which falls back to the next model
   - Call the selected provider within its model limit and remaining total budget; reserve time for a backup
   - Retry only connection/pool failures (max 2 retries); generation read/write timeouts may use a backup
   - Save image to disk (`storage/images/`)
   - Store metadata in Redis cache (7-day TTL)
   - Release the owned lock with bounded, cancellation-shielded cleanup
5. Response includes image URL served via FastAPI StaticFiles

## MCP interface

Mounted at `/mcp` (stateless HTTP). Two tools:
- `generate_image` -- same flow as REST, returns text summary with full URL
- `list_models` -- returns available model list

Mounted at `/v2/mcp` (stateless HTTP, `src/mcp/owned.py`), for products' own models:
- `generate_image` -- same generation flow with `OwnedTarget`: the image is saved by
  `services/image_store.py` (private dir + sqlite record owned by the caller's product),
  not in the static dir; the result carries the image id, mime and size but no URL.
  Edits take `source_image_id` (the caller's own image, read locally). Cache entries are
  separate from the legacy ones, and a hit is copied under a new id.
- Images are fetched / deleted with `GET` / `DELETE /v1/images/{id}` (`api/images.py`),
  owner product only.

`DisconnectMiddleware` is a plain ASGI wrapper with a bounded request-body relay.
It observes disconnects without competing with body / SSE readers, and attaches a
shared `RequestLifetime` to request state. REST and MCP pass that lifetime into the
same generation scope. This also cancels stateless MCP tools running in the SDK's
separate lifespan task group. SDK cancellation notifications propagate normally.

## Official billing (GET /v1/billing)

Product-key authentication determines the configured Ark resource ID. `api/billing.py`
calls `services/billing.py`, which signs read-only `ListSplitBillDetail` requests with
the official SDK core and sends them through bounded asynchronous httpx. The query
filters the Seedream product and API-key dimension, verifies every returned row,
and publishes only a complete monthly CNY summary. Redis provides a 15-minute
credential/product/resource/month cache and a global paced query lock. Missing
billing credentials disable only this optional endpoint. This flow never invokes
generation and does not change local usage estimates or MCP tools.

## Layer dependency rules

- `api/` and `mcp/` may import from `services/`, `models/`, `middleware/`, `config`
- `middleware/` may import from `models/`, `config`
- `services/` may import from `models/`, `config`
- `models/` is leaf -- no imports from other src packages
- `config.py` is leaf -- no imports from other src packages
