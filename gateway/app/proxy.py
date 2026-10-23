from __future__ import annotations

import logging
import time

import anyio
import httpx
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.auth import Caller
from app.config import Upstream
from app.usage import UsageRecord, UsageStore, now_iso

logger = logging.getLogger("gateway")

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# 调用方声明身份用的头只在网关这一层有效，转给上游前统一重写，不能被调用方直接塞给上游。
_CALLER_HEADERS = frozenset(
    {"authorization", "x-caller-app", "x-caller-user", "x-caller-scene", "x-image-user", "x-image-scene"}
)


def upstream_request_headers(
    service: str, headers: Headers, caller: Caller | None, client_host: str | None
) -> list[tuple[str, str]]:
    forwarded = [
        (name, value)
        for name, value in headers.items()
        if name not in _HOP_BY_HOP and name not in _CALLER_HEADERS and name not in {"host", "content-length"}
    ]
    if caller is not None:
        forwarded.extend(_caller_headers(service, headers, caller))
    prior = headers.get("x-forwarded-for")
    if client_host:
        forwarded = [(n, v) for n, v in forwarded if n != "x-forwarded-for"]
        forwarded.append(("x-forwarded-for", f"{prior}, {client_host}" if prior else client_host))
    return forwarded


def _caller_headers(service: str, headers: Headers, caller: Caller) -> list[tuple[str, str]]:
    forwarded = [("x-caller-app", caller.app_id)]
    if caller.user:
        forwarded.append(("x-caller-user", caller.user))
    if caller.scene:
        forwarded.append(("x-caller-scene", caller.scene))
    if service == "image":
        # image-service 自己按同一把产品密钥认定产品并按产品选方舟凭证，这里原样转交。
        forwarded.append(("authorization", headers["authorization"]))
        if caller.user:
            forwarded.append(("x-image-user", caller.user))
        if caller.scene:
            forwarded.append(("x-image-scene", caller.scene))
    return forwarded


def downstream_response_headers(service: str, upstream: Upstream, headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.multi_items():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP:
            continue
        if lowered == "location":
            value = _rewrite_location(service, upstream, value)
        result[name] = value
    return result


def _rewrite_location(service: str, upstream: Upstream, location: str) -> str:
    base = upstream.base_url.rstrip("/")
    if location.startswith(base):
        return f"/{service}{location[len(base) :]}"
    if location.startswith("/"):
        return f"/{service}{location}"
    return location


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status_code)


async def forward(
    request: Request,
    *,
    service: str,
    path: str,
    upstream: Upstream,
    client: httpx.AsyncClient,
    caller: Caller | None,
    body: bytes,
    usage: UsageStore,
) -> Response:
    """caller 为 None 只用于公开资源（生图结果图片），不带身份头、不记账。"""
    started = time.monotonic()

    async def finish(status: int, outcome: str) -> None:
        if caller is None:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        record = UsageRecord(
            ts=now_iso(),
            app_id=caller.app_id,
            user_id=caller.user,
            scene=caller.scene,
            request_id=caller.request_id,
            service=service,
            method=request.method,
            path=f"/{path}",
            status=status,
            outcome=outcome,
            duration_ms=duration_ms,
        )
        logger.info(
            "proxied",
            extra={
                "service": service,
                "app_id": caller.app_id,
                "path": record.path,
                "status": status,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "request_id": caller.request_id,
            },
        )
        try:
            await usage.record(record)
        except Exception:
            # 记账失败不能影响已经返回给调用方的结果，只留日志。
            logger.exception("usage_record_failed", extra={"service": service, "app_id": caller.app_id})

    upstream_request = client.build_request(
        request.method,
        httpx.URL(path=f"/{path}", query=request.url.query.encode("utf-8")),
        headers=upstream_request_headers(
            service, request.headers, caller, request.client.host if request.client else None
        ),
        content=body or None,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.PoolTimeout:
        await finish(503, "busy")
        return _error(503, "upstream_busy", f"{service} is at capacity, retry later.")
    except httpx.TimeoutException:
        await finish(504, "timeout")
        return _error(504, "upstream_timeout", f"{service} did not respond in time.")
    except httpx.TransportError:
        await finish(502, "unreachable")
        return _error(502, "upstream_unreachable", f"{service} is unreachable.")

    async def body_stream():
        outcome = "ok"
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        except httpx.TimeoutException:
            outcome = "timeout"
            raise
        except httpx.TransportError:
            outcome = "unreachable"
            raise
        finally:
            # 调用方收齐响应就断开时，Starlette 会取消整个请求任务；收尾和记账要屏蔽取消，
            # 否则还在线程池排队的记账会被直接撤掉，也不会留下任何报错。
            with anyio.CancelScope(shield=True):
                await upstream_response.aclose()
                await finish(upstream_response.status_code, outcome)

    return StreamingResponse(
        body_stream(),
        status_code=upstream_response.status_code,
        headers=downstream_response_headers(service, upstream, upstream_response.headers),
    )
