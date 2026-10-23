# Coding Conventions

## Language & Runtime
- Python 3.12+, using modern syntax (StrEnum, `X | None`, etc.)
- Package management: uv
- No legacy typing imports (`from __future__ import annotations` not needed)

## Style
- Formatter/linter: ruff
- Line length: 120
- Rules: E, F, I (isort), UP (pyupgrade), B (bugbear), SIM (simplify)
- Ignored: E501 (line length handled by formatter), B008 (FastAPI Depends)

## Framework Patterns
- FastAPI with async endpoints throughout
- pydantic-settings for configuration (single Settings class)
- pydantic BaseModel for all request/response schemas
- httpx.AsyncClient for outbound HTTP (with explicit timeouts)
- structlog for structured logging (JSON in prod)
- slowapi for rate limiting
- redis.asyncio for caching

## Project Structure Rules
- Providers implement the `SearchProvider` Protocol -- no base class inheritance
- New providers: add to `app/providers/`, register in `registry.py`
- Routes must use `registry.get_provider()`, never instantiate providers directly
- MCP tools must not import from routes
- No cross-imports between provider implementations
- Tests in `tests/`, using pytest + pytest-asyncio

## Naming
- Files: snake_case
- Classes: PascalCase
- Functions/variables: snake_case
- Constants: UPPER_SNAKE_CASE
- API endpoints: lowercase paths (`/search`, `/health`, `/providers`)

## Error Handling
- Provider errors: let httpx exceptions propagate (FastAPI returns 500)
- Fallback logic in route layer, not provider layer
- Cache failures: log warning, continue without cache

## Adding a New Provider
1. Create `app/providers/<name>.py` with a class implementing `SearchProvider`
2. Register in `app/providers/registry.py` `_init_providers()`
3. Add API key to `Settings` and `.env.example`
4. Run `python scripts/check_architecture.py` to verify layer rules
