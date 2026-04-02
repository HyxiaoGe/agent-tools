from __future__ import annotations

import asyncio
import base64
import re
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


async def call_openrouter(
    prompt: str,
    model: str,
    settings: Settings,
    style: str | None = None,
    aspect_ratio: str = "1:1",
    size: str = "1024x1024",
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Call OpenRouter API and return (image_bytes, format_ext)."""
    content = prompt
    if style:
        content = f"{prompt}, style: {style}"

    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": _map_size(size),
        },
        "stream": False,
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.openrouter_base_url}/chat/completions"

    logger.info("openrouter_request", model=model, aspect_ratio=aspect_ratio, size=size)

    if http_client is not None:
        resp = await http_client.post(url, headers=headers, json=payload)
    else:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        error_msg = "upstream error"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", error_msg)
        except Exception:
            pass
        raise GenerationError(f"Upstream provider error: {error_msg}")

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise GenerationError("No choices returned from upstream provider")

    message = choices[0].get("message", {})

    # Primary: OpenRouter returns images in message.images[]
    images = message.get("images", [])
    if images:
        for img in images:
            if img.get("type") == "image_url":
                data_url = img["image_url"]["url"]
                return _decode_data_url(data_url)

    # Fallback: some models return images in message.content[] (multimodal content parts)
    content_parts = message.get("content", [])
    if isinstance(content_parts, list):
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "image_url":
                data_url = part["image_url"]["url"]
                return _decode_data_url(data_url)

    raise GenerationError("No image content in upstream response")


def _map_size(size: str) -> str:
    """Map WxH size string to OpenRouter image_size parameter.

    Most models support 1K and 2K. 0.5K and 4K have limited support.
    """
    m = re.fullmatch(r"(\d+)x(\d+)", size)
    if not m:
        return "1K"
    max_dim = max(int(m.group(1)), int(m.group(2)))
    if max_dim <= 1024:
        return "1K"
    if max_dim <= 2048:
        return "2K"
    return "4K"


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a base64 data URL and return (bytes, extension)."""
    match = re.match(r"data:image/(\w+);base64,(.+)", data_url, re.DOTALL)
    if match:
        fmt = match.group(1)
        b64 = match.group(2)
    else:
        # Assume raw base64 PNG
        fmt = "png"
        b64 = data_url

    image_bytes = base64.b64decode(b64)
    return image_bytes, fmt


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
        # Another request is generating this image; wait and check cache
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
        # Call upstream
        image_bytes, fmt = await call_openrouter(
            prompt=request.prompt,
            model=model,
            settings=settings,
            style=request.style,
            aspect_ratio=request.aspect_ratio,
            size=request.size,
            http_client=http_client,
        )

        # Save to disk
        filename = save_image(image_bytes, cache_key, fmt, settings.storage_dir)

        gen_id = str(uuid.uuid4())
        image_url = f"{settings.static_url_prefix}/{filename}"
        created_at = datetime.now(timezone.utc).isoformat()

        # Store in cache
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
