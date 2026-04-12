# Coding Conventions

## Python

- Python 3.11+ (use `X | Y` union syntax, not `Optional[X]`)
- `from __future__ import annotations` at top of every module
- Pydantic v2 for all request/response models
- pydantic-settings for configuration
- structlog for logging (not stdlib logging directly)
- Async everywhere -- all endpoint handlers and service methods are async

## Linting and formatting

- Ruff for both linting and formatting
- Config in `pyproject.toml` under `[tool.ruff]`
- Line length: 120
- Rules: E, F, I (isort), UP (pyupgrade), B (bugbear), SIM (simplify)
- Ignored: E501 (line length handled by formatter), B008 (FastAPI Depends)

## Project structure

- Source code in `src/`, tests in `tests/`
- Tests organized as `tests/contract/` (API contract tests) and `tests/integration/`
- `conftest.py` at test root provides app fixture with fakeredis

## Error handling

- `GenerationError` exception class for all upstream/generation failures
- API endpoints catch errors and return `ErrorResponse` JSON with appropriate status codes
- Middleware functions return `JSONResponse | None` (None = pass through)

## Dependencies

- httpx for async HTTP (not aiohttp/requests)
- redis.asyncio for Redis operations
- fakeredis for test Redis

## Naming

- Router files named by resource (generate.py, health.py, models.py)
- Service files named by concern (generator.py, cache.py, metrics.py)
- All routers aggregated in `api/router.py` as `v1_router`
