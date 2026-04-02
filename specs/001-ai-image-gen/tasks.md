---
description: "Task list template for feature implementation"
---

# Tasks: AI Image Generation Middleware Service

**Input**: Design documents from `/specs/001-ai-image-gen/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

**Tests**: Tests are included as this is a new service with external API integration where contract and integration testing is critical for reliability.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, dependencies, configuration, and base structure

- [x] T001 Create project structure: `src/`, `src/models/`, `src/services/`, `src/api/`, `src/mcp/`, `src/middleware/`, `tests/`, `tests/contract/`, `tests/integration/`, `storage/images/` directories with `__init__.py` files
- [x] T002 Create `pyproject.toml` with dependencies: fastapi, uvicorn, httpx, redis[hiredis], pydantic-settings, structlog, fastmcp, python-multipart; dev deps: pytest, pytest-asyncio, httpx (test client), fakeredis, ruff
- [x] T003 [P] Create `.env.example` with all configuration variables documented (OPENROUTER_API_KEY, DEFAULT_MODEL, REDIS_URL, PORT, RATE_LIMIT_GLOBAL, RATE_LIMIT_PER_IP, CACHE_TTL_SECONDS, STORAGE_DIR, STATIC_URL_PREFIX)
- [x] T004 [P] Create `.gitignore` with entries for: `.env`, `storage/images/*`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`, `.ruff_cache/`
- [x] T005 [P] Create `ruff.toml` with Python 3.11 target, line-length 99, standard rule set

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can begin

**CRITICAL**: No user story work can begin until this phase is complete

- [x] T006 Implement Settings class in `src/config.py` using pydantic-settings: all env vars from .env.example, model registry as a class-level dict of ModelInfo objects, validators for required fields
- [x] T007 Implement Pydantic request/response models in `src/models/request.py`: GenerateRequest (prompt, model, aspect_ratio, size, style with validation) per data-model.md
- [x] T008 [P] Implement Pydantic response models in `src/models/response.py`: GenerateResponse (id, image_url, model, cached, created_at), ModelInfo (id, name, provider), HealthResponse (status, version, checks), ErrorResponse (error, message, retry_after) per data-model.md
- [x] T009 Implement Redis cache service in `src/services/cache.py`: async connect/disconnect, `get_cached(cache_key)`, `set_cached(cache_key, metadata, ttl)`, `acquire_lock(cache_key)`, `release_lock(cache_key)` — key format `image:{sha256}`, lock format `lock:image:{sha256}` per research.md R3
- [x] T010 Implement cache key computation utility in `src/services/cache.py`: SHA-256 of normalized `{prompt}:{model}:{aspect_ratio}:{size}` string
- [x] T011 Implement structlog configuration in `src/main.py` or `src/logging.py`: JSON output for production, colored console for dev, filter API keys from log output per research.md R6
- [x] T012 Implement FastAPI app factory with lifespan in `src/main.py`: create app, mount StaticFiles at `/static/images` from `STORAGE_DIR`, include v1 router, startup/shutdown for Redis connection, configure CORS
- [x] T013 [P] Create `tests/conftest.py` with shared fixtures: async test client (httpx.AsyncClient), fakeredis instance, mocked httpx transport for OpenRouter responses, temporary storage directory, Settings override

**Checkpoint**: Foundation ready — user story implementation can now begin

---

## Phase 3: User Story 1 — Generate Image via REST API (Priority: P1) MVP

**Goal**: Accept a text prompt via POST /v1/generate, call OpenRouter, decode base64 image, save to disk, cache in Redis, return image URL

**Independent Test**: Send POST with prompt → receive JSON with image_url → GET image_url returns image bytes

### Tests for User Story 1

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [x] T014 [P] [US1] Contract test for POST /v1/generate success path in `tests/contract/test_generate.py`: mock OpenRouter returns base64 image, assert 200 response matches GenerateResponse schema, assert image file exists at returned URL
- [x] T015 [P] [US1] Contract test for POST /v1/generate validation errors in `tests/contract/test_generate.py`: missing prompt → 422, invalid model → 400, invalid aspect_ratio → 422
- [x] T016 [P] [US1] Contract test for POST /v1/generate upstream error in `tests/contract/test_generate.py`: mock OpenRouter 500 → assert 502 with ErrorResponse schema
- [x] T017 [P] [US1] Integration test for cache deduplication in `tests/integration/test_cache_dedup.py`: generate with prompt+model, generate again with same → assert second response has `cached: true`, assert OpenRouter called only once

### Implementation for User Story 1

- [x] T018 [US1] Implement OpenRouter client in `src/services/generator.py`: async `generate_image(request: GenerateRequest) -> bytes` method that calls `POST {OPENROUTER_BASE_URL}/chat/completions` with `modalities: ["image"]`, parses content parts for image data URL, decodes base64, returns raw image bytes + detected format
- [x] T019 [US1] Implement image storage in `src/services/generator.py`: `save_image(image_bytes, cache_key, format) -> str` that writes to `{STORAGE_DIR}/{cache_key}.{ext}` and returns the relative URL path
- [x] T020 [US1] Implement generation orchestration in `src/services/generator.py`: `handle_generate(request: GenerateRequest) -> GenerateResponse` that computes cache key, checks cache, acquires lock on miss, calls OpenRouter, saves image, writes cache, releases lock, returns response
- [x] T021 [US1] Implement POST /v1/generate endpoint in `src/api/generate.py`: accepts GenerateRequest body, calls `handle_generate`, returns GenerateResponse, handles errors with ErrorResponse
- [x] T022 [US1] Wire generate router into v1 router in `src/api/router.py`: create v1 APIRouter, include generate endpoint, mount on app with `/v1` prefix

**Checkpoint**: User Story 1 fully functional — POST /v1/generate works end-to-end with caching

---

## Phase 4: User Story 2 — Generate Image via MCP Server (Priority: P2)

**Goal**: Expose generate_image and list_models as MCP tools via FastMCP, sharing the same generation service

**Independent Test**: Invoke generate_image MCP tool with prompt → receive image URL in response; invoke list_models → receive model list

### Tests for User Story 2

- [x] T023 [P] [US2] Contract test for generate_image MCP tool in `tests/contract/test_mcp_tools.py`: invoke tool with prompt, mock OpenRouter, assert response contains image URL text
- [x] T024 [P] [US2] Contract test for list_models MCP tool in `tests/contract/test_mcp_tools.py`: invoke tool, assert response contains all configured model names

### Implementation for User Story 2

- [x] T025 [US2] Implement FastMCP server with tools in `src/mcp/server.py`: create FastMCP("image-service"), define `generate_image` tool (calls `handle_generate`), define `list_models` tool (returns formatted model list from settings), per contracts/mcp-tools.md
- [x] T026 [US2] Mount MCP server on FastAPI app in `src/main.py`: `app.mount("/mcp", mcp.streamable_http_app())` per research.md R2

**Checkpoint**: MCP tools functional — generate_image and list_models work via /mcp endpoint

---

## Phase 5: User Story 3 — Browse Available Models (Priority: P2)

**Goal**: GET /v1/models returns the list of configured image generation models

**Independent Test**: GET /v1/models → receive JSON list of models with id, name, provider fields

### Tests for User Story 3

- [x] T027 [P] [US3] Contract test for GET /v1/models in `tests/contract/test_models.py`: assert 200 response contains models array, each with id/name/provider fields matching configured models

### Implementation for User Story 3

- [x] T028 [US3] Implement GET /v1/models endpoint in `src/api/models.py`: read model registry from settings, return `{"models": [ModelInfo, ...]}` per contracts/rest-api.md
- [x] T029 [US3] Include models router in v1 router in `src/api/router.py`

**Checkpoint**: Model listing works — GET /v1/models returns configured models

---

## Phase 6: User Story 4 — Rate Limiting (Priority: P3)

**Goal**: Enforce global (20/min) and per-IP (5/min) rate limits on the generation endpoint

**Independent Test**: Send 6 requests from same IP within 1 minute → 6th returns 429 with retry_after

### Tests for User Story 4

- [x] T030 [P] [US4] Integration test for per-IP rate limiting in `tests/integration/test_rate_limit.py`: send RATE_LIMIT_PER_IP + 1 requests, assert last returns 429 with retry_after header and ErrorResponse body
- [x] T031 [P] [US4] Integration test for global rate limiting in `tests/integration/test_rate_limit.py`: send RATE_LIMIT_GLOBAL + 1 requests from different IPs, assert last returns 429

### Implementation for User Story 4

- [x] T032 [US4] Implement rate limiting middleware in `src/middleware/rate_limit.py`: Redis sliding window counter per research.md R4, check global key `ratelimit:global:{window}` and per-IP key `ratelimit:ip:{ip}:{window}`, return 429 with Retry-After header when exceeded
- [x] T033 [US4] Apply rate limiting middleware to POST /v1/generate in `src/api/generate.py` or `src/api/router.py`: add as dependency or middleware on the generate route only

**Checkpoint**: Rate limiting active — abuse prevention working on generation endpoint

---

## Phase 7: User Story 5 — Health Monitoring (Priority: P3)

**Goal**: GET /v1/health returns service status with dependency checks (Redis, storage)

**Independent Test**: GET /v1/health → receive JSON with status and checks fields

### Tests for User Story 5

- [x] T034 [P] [US5] Contract test for GET /v1/health in `tests/contract/test_health.py`: assert 200 with HealthResponse schema when all deps healthy
- [x] T035 [P] [US5] Contract test for GET /v1/health degraded in `tests/contract/test_health.py`: mock Redis unavailable, assert response has status "degraded" and checks.redis contains error

### Implementation for User Story 5

- [x] T036 [US5] Implement GET /v1/health endpoint in `src/api/health.py`: ping Redis, check storage dir is writable, return HealthResponse with status healthy/degraded/unhealthy and individual check results, include service version from pyproject.toml
- [x] T037 [US5] Include health router in v1 router in `src/api/router.py`

**Checkpoint**: Health endpoint operational — monitoring systems can check service status

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Deployment, documentation, and production readiness

- [x] T038 [P] Create `Dockerfile`: Python 3.11-slim base, copy pyproject.toml + src/, install deps, expose 8090, CMD uvicorn
- [x] T039 [P] Create `docker-compose.yml`: image-service (build context, port 8090, env_file .env, volume for storage/images, depends_on redis), redis service, shared `middleware` network per user requirements
- [x] T040 [P] Add `storage/images/.gitkeep` to ensure directory exists in git while images are gitignored
- [x] T041 Validate quickstart.md by running through the steps in `specs/001-ai-image-gen/quickstart.md`: docker compose up, curl POST /v1/generate, curl GET /v1/models, curl GET /v1/health

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational — core MVP, start first
- **US2 (Phase 4)**: Depends on Foundational + US1 (reuses generator service)
- **US3 (Phase 5)**: Depends on Foundational only — can run parallel with US1
- **US4 (Phase 6)**: Depends on Foundational + US1 (applies to generate endpoint)
- **US5 (Phase 7)**: Depends on Foundational only — can run parallel with US1
- **Polish (Phase 8)**: Depends on all user stories complete

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Models before services
- Services before endpoints
- Core implementation before integration
- Story complete before marking checkpoint

### Parallel Opportunities

- T003, T004, T005 can run in parallel (Setup phase)
- T007, T008 can run in parallel (request vs response models)
- T014, T015, T016, T017 can all run in parallel (US1 tests)
- T023, T024 can run in parallel (US2 tests)
- T030, T031 can run in parallel (US4 tests)
- T034, T035 can run in parallel (US5 tests)
- T038, T039, T040 can run in parallel (Polish phase)
- US3 and US5 can run in parallel with US1 (independent of generation logic)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (POST /v1/generate with caching)
4. **STOP and VALIDATE**: Test US1 independently with curl
5. Deploy/demo if ready — basic image generation works

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. US1 → Test → Deploy (MVP: text-to-image generation)
3. US2 + US3 → Test → Deploy (MCP + model listing)
4. US4 + US5 → Test → Deploy (rate limiting + health)
5. Polish → Final deployment with Docker Compose

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story is independently completable and testable
- Verify tests fail before implementing
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
