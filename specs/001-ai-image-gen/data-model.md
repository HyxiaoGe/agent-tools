# Data Model: AI Image Generation Middleware

**Feature**: 001-ai-image-gen
**Date**: 2026-04-01

## Entities

### GenerateRequest

Represents an incoming image generation request from either the REST
API or MCP tool.

| Field | Type | Required | Default | Validation |
|-------|------|----------|---------|------------|
| prompt | string | yes | — | 1–10000 chars, stripped whitespace |
| model | string | no | Settings.DEFAULT_MODEL | Must be a key in SUPPORTED_MODELS |
| aspect_ratio | string | no | "1:1" | Must match pattern `\d+:\d+`, from allowed set |
| size | string | no | "1024x1024" | Must match pattern `\d+x\d+`, max 2048 per side |
| style | string | no | null | Free-text hint passed to upstream |

### GenerateResponse

Returned to the caller after generation or cache hit.

| Field | Type | Description |
|-------|------|-------------|
| id | string | Unique generation ID (UUID4) |
| image_url | string | Full URL to the served image file |
| model | string | Model identifier used for generation |
| cached | boolean | Whether the result was served from cache |
| created_at | string | ISO 8601 timestamp of generation |

### ModelInfo

Represents a supported image generation model.

| Field | Type | Description |
|-------|------|-------------|
| id | string | OpenRouter model identifier (e.g., `google/gemini-2.0-flash-exp:free`) |
| name | string | Human-readable display name |
| provider | string | Provider name (e.g., "Google", "Black Forest Labs", "OpenAI") |

### HealthResponse

Returned from the health check endpoint.

| Field | Type | Description |
|-------|------|-------------|
| status | string | "healthy", "degraded", or "unhealthy" |
| version | string | Service version from pyproject.toml |
| checks | object | Map of dependency name → status ("ok" or "error: ...") |

### ErrorResponse

Standard error envelope for all error responses.

| Field | Type | Description |
|-------|------|-------------|
| error | string | Machine-readable error code (e.g., "rate_limited", "generation_failed") |
| message | string | Human-readable error description |
| retry_after | integer | null | Seconds until retry (rate limit errors only) |

## Redis Data Structures

### Image Cache

- **Key pattern**: `image:{sha256_hex}` where sha256 is computed from
  `{prompt}:{model}:{aspect_ratio}:{size}` (normalized, lowercased)
- **Value**: JSON string
  ```json
  {
    "id": "uuid4",
    "file_path": "storage/images/{sha256_hex}.png",
    "image_url": "/static/images/{sha256_hex}.png",
    "model": "google/gemini-2.0-flash-exp:free",
    "created_at": "2026-04-01T12:00:00Z"
  }
  ```
- **TTL**: Configurable via `CACHE_TTL_SECONDS` (default 604800 = 7 days)

### Rate Limit Counters

- **Global key**: `ratelimit:global:{window_start_epoch}`
- **Per-IP key**: `ratelimit:ip:{client_ip}:{window_start_epoch}`
- **Value**: Integer counter (INCR)
- **TTL**: `RATE_LIMIT_WINDOW` seconds (default 60)

### Generation Lock (thundering herd)

- **Key pattern**: `lock:image:{sha256_hex}`
- **Value**: `1`
- **TTL**: 30 seconds (auto-expire if generation fails)
- **Set via**: SETNX (atomic)

## Relationships

```text
GenerateRequest ──(validates to)──> cache key (SHA-256)
                                        │
                    ┌───────────────────┤
                    ▼                    ▼
              [Cache HIT]          [Cache MISS]
                    │                    │
                    ▼                    ▼
            GenerateResponse     OpenRouter API call
                                        │
                                        ▼
                                  Decode base64
                                        │
                                        ▼
                                  Save to disk
                                        │
                                        ▼
                                  Write to Redis
                                        │
                                        ▼
                                 GenerateResponse
```

## Model Registry

Supported models are defined as a static configuration (Python dict
or list in `config.py`). No database needed. Adding a model means
adding an entry to the registry and redeploying.

| Model ID | Display Name | Provider |
|----------|-------------|----------|
| `google/gemini-2.0-flash-exp:free` | Gemini 2.0 Flash | Google |
| `black-forest-labs/flux-1.1-pro` | Flux 1.1 Pro | Black Forest Labs |
| `openai/gpt-5` | GPT-5 Image | OpenAI |

Note: Exact model IDs will be confirmed against OpenRouter's model
listing at implementation time and may need adjustment.
