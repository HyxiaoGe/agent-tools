<!--
Sync Impact Report
===================
Version change: N/A → 1.0.0 (initial ratification)
Modified principles: N/A (initial)
Added sections:
  - Core Principles (5 principles)
  - Technology Constraints
  - Development Workflow
  - Governance
Removed sections: N/A
Templates requiring updates:
  - .specify/templates/plan-template.md ✅ compatible (Constitution Check
    section is generic and will pick up these principles)
  - .specify/templates/spec-template.md ✅ compatible (no constitution-
    specific references to update)
  - .specify/templates/tasks-template.md ✅ compatible (phase structure
    aligns with principles)
Follow-up TODOs: None
-->

# Image Service Constitution

## Core Principles

### I. Performance-First

All request paths MUST be optimized for low latency and efficient
resource usage:

- External API calls (OpenRouter) MUST use async I/O; blocking calls
  are prohibited in request handlers.
- Redis MUST be used as the caching layer for generated images and
  API responses. Cache-hit paths MUST avoid upstream API calls.
- Response times for cache hits MUST target < 50ms p95.
- Generation requests MUST return promptly via async patterns
  (polling, webhooks, or SSE) when upstream latency exceeds 5s.
- Resource-intensive operations MUST NOT block the event loop.

### II. Simplicity & YAGNI

Start minimal. Add complexity only when a concrete requirement
demands it:

- Every new abstraction, dependency, or layer MUST be justified by a
  current requirement — not a hypothetical future one.
- Prefer flat module structures over deep nesting.
- Three similar lines of code are better than a premature abstraction.
- Configuration MUST use environment variables with sensible defaults;
  no custom config frameworks unless proven necessary.
- Docker Compose is the deployment target. Do not introduce
  orchestration tooling (Kubernetes, Nomad) unless explicitly scoped.

### III. API Contract Stability

The REST API and MCP Server are the project's public interfaces.
Changes MUST be managed carefully:

- All endpoints MUST be versioned (e.g., `/v1/...`).
- Breaking changes MUST increment the API major version and include
  a migration note in the changelog.
- MCP tool definitions MUST maintain backward compatibility; removing
  or renaming a tool is a breaking change.
- Request/response schemas MUST be validated with Pydantic models.
  Schema changes MUST be reflected in both REST and MCP interfaces
  where applicable.

### IV. Secure Defaults

API keys and user-supplied inputs are the primary attack surface:

- OpenRouter API keys MUST be loaded from environment variables or
  a secrets manager. Keys MUST NOT appear in logs, error messages,
  or responses.
- All user-supplied inputs (prompts, parameters, image URLs) MUST be
  validated and sanitized before being forwarded to upstream APIs.
- Rate limiting MUST be enforced on generation endpoints to prevent
  abuse and upstream cost overruns.
- CORS and authentication mechanisms MUST be configured explicitly;
  open-by-default is prohibited for production deployments.

### V. Test-Driven Quality

Critical paths MUST have automated test coverage:

- API endpoint contracts MUST have integration tests that verify
  request/response schemas.
- External API calls (OpenRouter) MUST be tested with deterministic
  mocks; tests MUST NOT make real upstream calls.
- Cache behavior (hit/miss/expiry) MUST be tested explicitly.
- MCP tool invocations MUST have contract tests verifying input
  schemas and output formats.

## Technology Constraints

- **Language**: Python 3.11+
- **Web framework**: FastAPI (async)
- **MCP framework**: FastMCP
- **Cache / queue**: Redis
- **Image generation**: OpenRouter API
- **Validation**: Pydantic v2
- **Testing**: pytest + pytest-asyncio + httpx (AsyncClient)
- **Containerization**: Docker + Docker Compose
- **Linting**: ruff
- **Type checking**: Optional but encouraged (mypy or pyright)

New dependencies MUST be justified in the implementation plan.
Vendored or forked dependencies are prohibited unless upstream is
unmaintained.

## Development Workflow

- Feature branches follow the `###-feature-name` convention managed
  by Speckit.
- Every feature MUST go through: Specify → Plan → Tasks → Implement.
- Pull requests MUST pass all automated tests before merge.
- Environment-specific configuration (API keys, Redis URLs) MUST
  live in `.env` files that are gitignored. A `.env.example` MUST
  document all required variables.
- Docker Compose MUST be the canonical way to run the full stack
  locally. `docker compose up` MUST yield a working service with no
  additional manual steps beyond populating `.env`.

## Governance

This constitution is the authoritative source of project-wide rules.
It supersedes ad-hoc decisions when conflicts arise.

- **Amendments**: Any change to principles or constraints MUST be
  documented with a version bump, rationale, and migration plan if
  the change affects existing code.
- **Versioning**: Constitution versions follow SemVer — MAJOR for
  principle removals or incompatible redefinitions, MINOR for new
  principles or material expansions, PATCH for clarifications.
- **Compliance**: All implementation plans MUST include a
  Constitution Check section verifying alignment with these
  principles. Reviewers MUST flag violations before merge.

**Version**: 1.0.0 | **Ratified**: 2026-04-01 | **Last Amended**: 2026-04-01
