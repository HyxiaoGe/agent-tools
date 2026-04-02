# REST API Contract: Image Service v1

**Base URL**: `http://{host}:8090/v1`

## POST /v1/generate

Generate an image from a text prompt.

### Request

**Content-Type**: `application/json`

```json
{
  "prompt": "A watercolor painting of a mountain sunset",
  "model": "google/gemini-2.0-flash-exp:free",
  "aspect_ratio": "16:9",
  "size": "1024x576",
  "style": "watercolor"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| prompt | string | yes | — | Text description of the image to generate |
| model | string | no | configured default | OpenRouter model ID |
| aspect_ratio | string | no | "1:1" | Aspect ratio (e.g., "16:9", "4:3") |
| size | string | no | "1024x1024" | Output dimensions as "WxH" |
| style | string | no | null | Style hint (e.g., "watercolor", "photorealistic") |

### Response — 200 OK

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "image_url": "http://localhost:8090/static/images/abc123def456.png",
  "model": "google/gemini-2.0-flash-exp:free",
  "cached": false,
  "created_at": "2026-04-01T12:00:00Z"
}
```

### Response — 422 Validation Error

```json
{
  "error": "validation_error",
  "message": "prompt: field required",
  "retry_after": null
}
```

### Response — 429 Rate Limited

```json
{
  "error": "rate_limited",
  "message": "Rate limit exceeded. Try again in 45 seconds.",
  "retry_after": 45
}
```

### Response — 502 Upstream Error

```json
{
  "error": "generation_failed",
  "message": "Upstream provider returned an error: insufficient credits",
  "retry_after": null
}
```

---

## GET /v1/models

List available image generation models.

### Response — 200 OK

```json
{
  "models": [
    {
      "id": "google/gemini-2.0-flash-exp:free",
      "name": "Gemini 2.0 Flash",
      "provider": "Google"
    },
    {
      "id": "black-forest-labs/flux-1.1-pro",
      "name": "Flux 1.1 Pro",
      "provider": "Black Forest Labs"
    },
    {
      "id": "openai/gpt-5",
      "name": "GPT-5 Image",
      "provider": "OpenAI"
    }
  ]
}
```

---

## GET /v1/health

Service health check.

### Response — 200 OK (healthy)

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "checks": {
    "redis": "ok",
    "storage": "ok"
  }
}
```

### Response — 200 OK (degraded)

```json
{
  "status": "degraded",
  "version": "0.1.0",
  "checks": {
    "redis": "error: connection refused",
    "storage": "ok"
  }
}
```

---

## Static Files

**Mount path**: `/static/images/`
**Source**: `storage/images/` directory

Generated images are accessible directly at their URL without
authentication. Example:

```
GET /static/images/abc123def456.png
```

---

## Common Headers

All responses include:
- `Content-Type: application/json` (API endpoints)
- `X-Request-ID: {uuid}` (for log correlation)

Rate-limited responses include:
- `Retry-After: {seconds}`

---

## Error Envelope

All error responses use this consistent format:

```json
{
  "error": "string — machine-readable error code",
  "message": "string — human-readable description",
  "retry_after": "integer or null"
}
```

Error codes:
- `validation_error` — invalid request parameters (422)
- `rate_limited` — rate limit exceeded (429)
- `generation_failed` — upstream provider error (502)
- `model_not_found` — unknown model ID (400)
- `internal_error` — unexpected server error (500)
