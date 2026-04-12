from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse

from src.models.response import ErrorResponse


async def check_rate_limit(request: Request) -> JSONResponse | None:
    """Check rate limits. Returns JSONResponse if limited, None if OK."""
    settings = request.app.state.settings
    redis = request.app.state.redis

    window = int(time.time()) // settings.rate_limit_window
    # Prefer X-Forwarded-For for reverse proxy deployments (take first/leftmost IP)
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")

    global_key = f"ratelimit:global:{window}"
    ip_key = f"ratelimit:ip:{client_ip}:{window}"

    pipe = redis.pipeline()
    pipe.incr(global_key)
    pipe.expire(global_key, settings.rate_limit_window)
    pipe.incr(ip_key)
    pipe.expire(ip_key, settings.rate_limit_window)
    results = await pipe.execute()

    global_count = results[0]
    ip_count = results[2]

    retry_after = settings.rate_limit_window - (int(time.time()) % settings.rate_limit_window)

    if ip_count > settings.rate_limit_per_ip:
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error="rate_limited",
                message=f"Per-IP rate limit exceeded ({settings.rate_limit_per_ip}/min). "
                f"Try again in {retry_after} seconds.",
                retry_after=retry_after,
            ).model_dump(),
            headers={"Retry-After": str(retry_after)},
        )

    if global_count > settings.rate_limit_global:
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error="rate_limited",
                message=f"Global rate limit exceeded ({settings.rate_limit_global}/min). "
                f"Try again in {retry_after} seconds.",
                retry_after=retry_after,
            ).model_dump(),
            headers={"Retry-After": str(retry_after)},
        )

    return None
