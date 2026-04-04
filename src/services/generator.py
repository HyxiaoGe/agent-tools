from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from src.config import SUPPORTED_MODELS, Settings
from src.models.request import GenerateRequest
from src.models.response import GenerateResponse
from src.services.cache import CacheService, compute_cache_key

logger = structlog.get_logger()


class GenerationError(Exception):
    """Raised when upstream image generation fails."""

    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def call_gemini(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Call Google Gemini API directly and return (image_bytes, format_ext)."""
    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload = {
        "contents": [{"parts": [{"text": content}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }

    url = f"{settings.google_api_base_url}/models/{model}:generateContent?key={settings.google_api_key}"

    logger.info("gemini_request", model=model)

    if http_client is not None:
        resp = await http_client.post(
            url, headers={"Content-Type": "application/json"}, json=payload
        )
    else:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url, headers={"Content-Type": "application/json"}, json=payload
            )

    if resp.status_code != 200:
        error_msg = "upstream error"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", error_msg)
        except Exception:
            pass
        raise GenerationError(f"Gemini API error: {error_msg}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise GenerationError("No candidates returned from Gemini")

    parts = candidates[0].get("content", {}).get("parts", [])

    # Find image part (inlineData)
    for part in parts:
        if "inlineData" in part:
            inline = part["inlineData"]
            mime_type = inline.get("mimeType", "image/png")
            image_bytes = base64.b64decode(inline["data"])
            # Extract format from mimeType: "image/jpeg" -> "jpeg"
            fmt = mime_type.split("/")[-1] if "/" in mime_type else "png"
            return image_bytes, fmt

    raise GenerationError("No image content in Gemini response")


def save_image(image_bytes: bytes, cache_key: str, fmt: str, storage_dir: str) -> str:
    """Save image to disk and return the relative URL path."""
    filename = f"{cache_key}.{fmt}"
    path = Path(storage_dir) / filename
    path.write_bytes(image_bytes)
    logger.info("image_saved", path=str(path), size=len(image_bytes))
    return filename


async def handle_generate(
    request: GenerateRequest,
    settings: Settings,
    cache: CacheService,
    http_client: httpx.AsyncClient | None = None,
) -> GenerateResponse:
    """Orchestrate generation: cache check → generate → save → cache set."""
    model = request.model or settings.default_model

    # Validate model
    if model not in SUPPORTED_MODELS:
        raise GenerationError(
            f"Model '{model}' not found. Use GET /v1/models for available models.",
            status_code=400,
        )

    cache_key = compute_cache_key(
        request.prompt, model, request.aspect_ratio, request.size, request.style
    )

    # Check cache
    cached = await cache.get_cached(cache_key)
    if cached is not None:
        return GenerateResponse(
            id=cached["id"],
            image_url=cached["image_url"],
            model=cached["model"],
            cached=True,
            created_at=cached["created_at"],
        )

    # Acquire lock (thundering herd protection)
    lock_acquired = await cache.acquire_lock(cache_key)
    if not lock_acquired:
        for _ in range(20):
            await asyncio.sleep(0.5)
            cached = await cache.get_cached(cache_key)
            if cached is not None:
                return GenerateResponse(
                    id=cached["id"],
                    image_url=cached["image_url"],
                    model=cached["model"],
                    cached=True,
                    created_at=cached["created_at"],
                )
        raise GenerationError("Generation timed out waiting for lock")

    try:
        image_bytes, fmt = await call_gemini(
            prompt=request.prompt,
            model=model,
            settings=settings,
            style=request.style,
            http_client=http_client,
        )

        filename = save_image(image_bytes, cache_key, fmt, settings.storage_dir)

        gen_id = str(uuid.uuid4())
        image_url = f"{settings.static_url_prefix}/{filename}"
        created_at = datetime.now(timezone.utc).isoformat()

        metadata = {
            "id": gen_id,
            "image_url": image_url,
            "model": model,
            "created_at": created_at,
        }
        await cache.set_cached(cache_key, metadata)

        return GenerateResponse(
            id=gen_id,
            image_url=image_url,
            model=model,
            cached=False,
            created_at=created_at,
        )
    finally:
        await cache.release_lock(cache_key)
