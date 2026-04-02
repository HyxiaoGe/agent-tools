# Implementation Plan: AI Image Generation Middleware Service

**Branch**: `001-ai-image-gen` | **Date**: 2026-04-01 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-ai-image-gen/spec.md`

## Summary

Build an AI image generation middleware that wraps the OpenRouter
`/api/v1/chat/completions` endpoint (with `modalities: ["image"]`) behind
a versioned REST API (`POST /v1/generate`, `GET /v1/models`,
`GET /v1/health`) and an MCP Server (`generate_image`, `list_models`
tools). Images are returned as base64 from OpenRouter, decoded and saved
to `storage/images/`, and served via FastAPI StaticFiles. Redis provides
prompt+model deduplication caching. Rate limiting, structured logging,
and Docker Compose deployment complete the feature set.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: FastAPI, FastMCP, httpx (async), Redis
(via redis-py async), pydantic-settings, structlog
**Storage**: Local filesystem (`storage/images/`) + Redis (cache)
**Testing**: pytest + pytest-asyncio + httpx.AsyncClient + fakeredis
**Target Platform**: Linux container (Docker), exposed on port 8090
**Project Type**: Web service (REST API + MCP Server)
**Performance Goals**: Cache-hit responses < 50ms p95; generation
requests bounded by upstream latency (10-60s typical)
**Constraints**: Single-instance deployment; local file storage only;
synchronous request-response (no async job queue)
**Scale/Scope**: Single developer, blog-usage volume (~100 generations/day)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Gate | Status |
|---|-----------|------|--------|
| I | Performance-First | All OpenRouter calls use httpx async client; Redis async driver; no blocking I/O in handlers | PASS |
| II | Simplicity & YAGNI | Flat module structure; pydantic-settings for config (env vars); Docker Compose only; no ORM, no job queue | PASS |
| III | API Contract Stability | REST endpoints versioned under `/v1/`; Pydantic request/response models; MCP tool schemas defined | PASS |
| IV | Secure Defaults | API key from env var; input validation via Pydantic; rate limiting on `/v1/generate`; CORS explicit | PASS |
| V | Test-Driven Quality | Contract tests for REST + MCP; mocked OpenRouter via httpx mock; Redis cache tests via fakeredis | PASS |

All gates pass. No violations to track.

## Project Structure

### Documentation (this feature)

```text
specs/001-ai-image-gen/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── rest-api.md
│   └── mcp-tools.md
└── tasks.md
```

### Source Code (repository root)

```text
src/
├── __init__.py
├── main.py              # FastAPI app factory + lifespan
├── config.py            # pydantic-settings Settings class
├── models/
│   ├── __init__.py
│   ├── request.py       # GenerateRequest, etc.
│   └── response.py      # GenerateResponse, ModelInfo, HealthResponse
├── services/
│   ├── __init__.py
│   ├── generator.py     # OpenRouter client + generation logic
│   └── cache.py         # Redis cache operations
├── api/
│   ├── __init__.py
│   ├── router.py        # v1 router aggregation
│   ├── generate.py      # POST /v1/generate
│   ├── models.py        # GET /v1/models
│   └── health.py        # GET /v1/health
├── mcp/
│   ├── __init__.py
│   └── server.py        # FastMCP server with tools
└── middleware/
    ├── __init__.py
    └── rate_limit.py    # Rate limiting middleware

tests/
├── conftest.py          # Shared fixtures (async client, fakeredis, mocked httpx)
├── contract/
│   ├── test_generate.py
│   ├── test_models.py
│   ├── test_health.py
│   └── test_mcp_tools.py
└── integration/
    ├── test_cache_dedup.py
    └── test_rate_limit.py

storage/
└── images/              # Generated images (gitignored, Docker volume)

docker-compose.yml
Dockerfile
.env.example
pyproject.toml
```

**Structure Decision**: Single project layout. The service is a single
FastAPI process that also runs the FastMCP server. Flat `src/` with
`api/`, `mcp/`, `models/`, `services/`, `middleware/` sub-packages.
No separate frontend or multi-service split needed.

## Complexity Tracking

> No violations. All gates pass.
