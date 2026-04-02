from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.middleware.auth import check_api_key
from src.middleware.rate_limit import check_rate_limit
from src.models.request import GenerateRequest
from src.models.response import ErrorResponse, GenerateResponse
from src.services.generator import GenerationError, handle_generate

router = APIRouter()


@router.post(
    "/generate",
    response_model=GenerateResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def generate(body: GenerateRequest, request: Request) -> GenerateResponse | JSONResponse:
    # API key check
    auth_response = check_api_key(request)
    if auth_response is not None:
        return auth_response

    # Rate limiting
    rate_limit_response = await check_rate_limit(request)
    if rate_limit_response is not None:
        return rate_limit_response

    settings = request.app.state.settings
    cache = request.app.state.cache

    try:
        http_client = getattr(request.app.state, "http_client", None)
        return await handle_generate(body, settings, cache, http_client=http_client)
    except GenerationError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ErrorResponse(
                error="model_not_found" if e.status_code == 400 else "generation_failed",
                message=e.message,
            ).model_dump(),
        )
