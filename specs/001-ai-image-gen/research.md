# Research: AI Image Generation Middleware

**Feature**: 001-ai-image-gen
**Date**: 2026-04-01

## R1: OpenRouter Image Generation API

**Decision**: Use OpenRouter `/api/v1/chat/completions` with
`modalities: ["image"]` parameter to request image generation from
models like Gemini, Flux, and GPT-5 Image.

**Rationale**: OpenRouter provides a unified chat-completions-compatible
interface across multiple image generation providers. Using the standard
chat completions endpoint with the `modalities` parameter means we can
reuse familiar request/response patterns. Images are returned as base64
data URLs in the assistant message content.

**Request format**:
```json
{
  "model": "google/gemini-2.0-flash-exp:free",
  "messages": [
    {
      "role": "user",
      "content": "Generate an image of a sunset over mountains"
    }
  ],
  "modalities": ["image"],
  "max_tokens": 4096
}
```

**Response format**: The response follows the chat completions schema.
Image content appears as a content part with `type: "image_url"` and
the image data as a base64 data URL in `image_url.url`. The service
MUST parse the content parts array, extract image data URLs, decode the
base64 payload, and save to disk.

**Alternatives considered**:
- Direct provider APIs (Stability, DALL-E): Requires separate clients
  per provider; defeats the purpose of a unified middleware.
- Replicate API: Good multi-model support but different API shape and
  pricing model.

## R2: FastMCP Integration with FastAPI

**Decision**: Run FastMCP server alongside FastAPI in the same process.
FastMCP provides a `streamable-http` transport that can be mounted into the
FastAPI app as an ASGI sub-application.

**Rationale**: FastMCP's `streamable-http` transport is ASGI-compatible,
allowing it to be mounted at a path (e.g., `/mcp`) on the FastAPI app.
This shares the same process, event loop, and service instances — no
IPC or separate deployment needed.

**Integration pattern**:
```python
from fastmcp import FastMCP
from fastapi import FastAPI

mcp = FastMCP("image-service")
app = FastAPI()

# Mount MCP server at /mcp
app.mount("/mcp", mcp.streamable_http_app())
```

MCP tools call the same `generator` service that the REST endpoints use.

**Alternatives considered**:
- Separate MCP process with stdio transport: Adds deployment complexity;
  cannot share cache/state without IPC.
- SSE transport: Deprecated in favor of streamable-http in MCP spec.

## R3: Redis Caching Strategy

**Decision**: Use Redis with key = SHA-256 hash of
`{prompt}:{model}:{aspect_ratio}:{size}` and value = JSON containing
the image file path and metadata. TTL configurable via env var
(default: 7 days).

**Rationale**: SHA-256 produces a fixed-length key regardless of prompt
length. Including all generation parameters in the hash ensures that
different parameter combinations are cached separately. Storing the
file path (not the image data) in Redis keeps cache entries small and
avoids Redis memory pressure.

**Cache flow**:
1. Compute cache key from normalized request parameters.
2. Check Redis for existing entry.
3. If hit: return stored file path/URL immediately.
4. If miss: call OpenRouter, decode base64, save file, store metadata
   in Redis, return URL.

**Thundering herd mitigation**: Use a simple Redis SETNX lock with short
TTL (30s) on the cache key + `:lock` suffix. If lock acquired, proceed
with generation. If lock exists, poll with short backoff until result
appears or lock expires.

**Alternatives considered**:
- File-system-based dedup (check if file exists by hash name): No TTL
  control, no atomic check-and-set, slower lookups.
- In-memory dict: Lost on restart, no sharing if multiple workers.

## R4: Rate Limiting Approach

**Decision**: Implement rate limiting using Redis sliding window
counters. Global limit: 20 req/min. Per-IP limit: 5 req/min. Applied
as FastAPI middleware on the `/v1/generate` endpoint only.

**Rationale**: Redis-backed rate limiting is consistent across restarts
and (future) multiple workers. Sliding window provides smoother
throttling than fixed windows. Applying only to the generation endpoint
avoids unnecessarily limiting model listing and health checks.

**Implementation**: Use Redis INCR + EXPIRE pattern with a 60-second
window key. Two keys per request:
- `ratelimit:global:{window}` — global counter
- `ratelimit:ip:{ip}:{window}` — per-IP counter

Return `429 Too Many Requests` with `Retry-After` header when exceeded.

**Alternatives considered**:
- slowapi / limits library: Additional dependency for a simple use case;
  YAGNI principle favors a minimal custom implementation.
- Nginx-level rate limiting: Adds infrastructure complexity; not needed
  for single-instance deployment.

## R5: Image Storage and Serving

**Decision**: Save decoded images to `storage/images/{hash}.{ext}` where
hash is the same SHA-256 used for the cache key. Serve via FastAPI
`StaticFiles` mounted at `/static/images/`.

**Rationale**: Using the cache hash as the filename provides natural
deduplication at the filesystem level. StaticFiles is zero-config
static serving built into FastAPI. The `storage/` directory is mounted
as a Docker volume for persistence across container restarts.

**Image URL format**: `http://{host}:{port}/static/images/{hash}.png`

**File format detection**: Parse the base64 data URL prefix
(`data:image/png;base64,...`) to determine the file extension. Default
to `.png` if prefix is missing or unrecognized.

**Alternatives considered**:
- Nginx for static serving: Extra process; unnecessary for blog-scale
  traffic.
- S3/MinIO: Out of scope per assumptions; can be added later behind a
  storage abstraction if needed.

## R6: Structured Logging

**Decision**: Use `structlog` with JSON output for production and
colored console output for development. Log generation requests
(without the full prompt to avoid PII leakage), cache hits/misses,
rate limit events, and errors.

**Rationale**: structlog integrates cleanly with Python's stdlib logging
and produces machine-parseable JSON logs suitable for Docker log
drivers. Per constitution principle IV, API keys MUST NOT appear in logs.

**Alternatives considered**:
- loguru: Popular but structlog's stdlib integration is better for
  library/middleware contexts.
- stdlib logging only: Lacks structured output without significant
  boilerplate.

## R7: Configuration Management

**Decision**: Use `pydantic-settings` with a single `Settings` class.
All config from environment variables with `.env` file support.

**Key settings**:
- `OPENROUTER_API_KEY` (required, secret)
- `OPENROUTER_BASE_URL` (default: `https://openrouter.ai/api/v1`)
- `DEFAULT_MODEL` (default: `google/gemini-2.0-flash-exp:free`)
- `REDIS_URL` (default: `redis://redis:6379/0`)
- `STORAGE_DIR` (default: `storage/images`)
- `STATIC_URL_PREFIX` (default: `/static/images`)
- `RATE_LIMIT_GLOBAL` (default: `20`)
- `RATE_LIMIT_PER_IP` (default: `5`)
- `RATE_LIMIT_WINDOW` (default: `60`)
- `CACHE_TTL_SECONDS` (default: `604800` — 7 days)
- `HOST` (default: `0.0.0.0`)
- `PORT` (default: `8090`)

**Alternatives considered**:
- python-dotenv + manual parsing: More boilerplate, no validation.
- dynaconf: Over-featured for this use case; YAGNI.
