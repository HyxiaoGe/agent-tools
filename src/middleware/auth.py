from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from src.models.response import ErrorResponse


def check_api_key(request: Request) -> JSONResponse | None:
    """Check Bearer token if API_KEY is configured. Returns None if OK."""
    settings = request.app.state.settings
    if not settings.api_key:
        return None  # No auth configured, allow all

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="unauthorized",
                message="Missing or invalid Authorization header. Use: Bearer <API_KEY>",
            ).model_dump(),
        )

    token = auth_header[7:]  # Strip "Bearer "
    if token != settings.api_key:
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="unauthorized",
                message="Invalid API key.",
            ).model_dump(),
        )

    return None
