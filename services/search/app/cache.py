import hashlib
import json

import redis.asyncio as redis

from app.config import settings
from app.models import SearchRequest, SearchResponse, SearchType

_redis: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def build_cache_key(provider: str, request: SearchRequest) -> str:
    raw = {
        "version": 3,
        "provider": provider,
        "type": request.type.value,
        "query": request.query.lower().strip(),
        "lang": request.lang.lower().strip(),
        "region": request.region.lower().strip(),
        "region_explicit": "region" in request.model_fields_set,
        "freshness": request.freshness,
        "count": request.count,
        "page": request.page,
        "domain_filters": sorted(request.domain_filters),
    }
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"search:v3:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _ttl_for_type(search_type: SearchType) -> int:
    return {
        SearchType.WEB: settings.CACHE_TTL_WEB,
        SearchType.NEWS: settings.CACHE_TTL_NEWS,
        SearchType.IMAGE: settings.CACHE_TTL_IMAGE,
    }[search_type]


async def get_cached(provider: str, request: SearchRequest) -> SearchResponse | None:
    r = await get_redis()
    key = build_cache_key(provider, request)
    data = await r.get(key)
    if data is None:
        return None
    resp = SearchResponse.model_validate_json(data)
    resp.cached = True
    resp.credits_used = None
    resp.cache_key_version = 3
    return resp


async def set_cached(provider: str, request: SearchRequest, response: SearchResponse) -> None:
    r = await get_redis()
    key = build_cache_key(provider, request)
    ttl = _ttl_for_type(request.type)
    await r.setex(key, ttl, response.model_dump_json())


async def flush_cache() -> int:
    r = await get_redis()
    keys = [k async for k in r.scan_iter("search:*")]
    if keys:
        await r.delete(*keys)
    return len(keys)
