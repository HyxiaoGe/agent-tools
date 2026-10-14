"""
reader-service — 网页内容读取微服务
先直接抓取 HTML 提取正文；直接抓取失败或迟迟没有结果时，同时请求 Tavily Extract
与 Jina Reader，谁先读到用谁，整个请求受同一个总时限约束。
"""

import asyncio
import logging
import os
import time
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from direct_read import read_direct

app = FastAPI(title="Reader Service")
logger = logging.getLogger("reader_service")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

JINA_BASE_URL = "https://r.jina.ai"
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
JINA_CONNECT_TIMEOUT = float(os.getenv("JINA_CONNECT_TIMEOUT", "3"))
JINA_MIN_READ_TIMEOUT = 10.0
JINA_READ_TIMEOUT = max(
    JINA_MIN_READ_TIMEOUT,
    float(os.getenv("JINA_READ_TIMEOUT", os.getenv("JINA_TIMEOUT", "16"))),
)
JINA_WRITE_TIMEOUT = float(os.getenv("JINA_WRITE_TIMEOUT", "3"))
JINA_POOL_TIMEOUT = float(os.getenv("JINA_POOL_TIMEOUT", "2"))
# 一次读取的总时限：直接抓取、Tavily、Jina 都在这个时限内完成，超时就放弃该网页。
# 调用方会并行读多个网页、等最慢的那个，读不到的网页要尽快放弃，免得拖住整轮。
READ_TOTAL_TIMEOUT = float(os.getenv("READ_TOTAL_TIMEOUT", "10"))
# 直接抓取在这段时间内没有结果（或已经失败）就同时请求 Tavily 和 Jina；直接抓取继续跑，三路谁先读到用谁。
DIRECT_HEDGE_DELAY = float(os.getenv("DIRECT_HEDGE_DELAY", "1.5"))
JINA_RETRY_BACKOFF = float(os.getenv("JINA_RETRY_BACKOFF", "0.2"))
# Jina 首次失败后剩余时间不足这么多就不再重试，重试也来不及读完。
JINA_MIN_RETRY_WINDOW = float(os.getenv("JINA_MIN_RETRY_WINDOW", "4"))
JINA_MAX_ATTEMPTS = 2
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
# 号池：逗号分隔的多个 key，轮询使用；兼容单 key 的 TAVILY_API_KEY
TAVILY_API_KEYS = [
    key.strip()
    for key in os.getenv("TAVILY_API_KEYS", os.getenv("TAVILY_API_KEY", "")).split(",")
    if key.strip()
]
# 实测 Tavily p90 约 3s；同时还受整次读取的总时限约束
TAVILY_TIMEOUT = float(os.getenv("TAVILY_TIMEOUT", "8"))
# Tavily 账户级失败按 key 冷却：密钥无效一直停用到重启；额度用完隔天再试
# （各账户计费周期不同，按天重试最省事且失败很快）；限流短暂冷却。
TAVILY_KEY_COOLDOWNS = {401: None, 432: 86400.0, 433: 86400.0, 429: 60.0}
GOOGLE_FAVICON_API = "https://www.google.com/s2/favicons?sz=32&domain="
# Jina 账户级失败（密钥失效/余额不足）：换哪个网页都读不了，需要人处理
ACCOUNT_FAILURE_STATUSES = (401, 402, 403)

# 最近一次读取暴露的账户级故障；/health 据此报 503 让哨兵告警，下次读取成功即清除。
# 只在真实读取时更新，健康检查本身不调用 Jina、不消耗额度。
_account_failure: dict | None = None


class ReadResponse(BaseModel):
    url: str
    title: str | None = None
    content: str
    favicon: str | None = None
    content_length: int
    fetch_ms: int
    attempts: int = 1
    provider: str = "jina"


def _monotonic() -> float:
    return time.monotonic()


def _timeout_for(remaining_seconds: float) -> httpx.Timeout:
    """让每个阶段的超时都受本次请求剩余总预算约束。"""
    return httpx.Timeout(
        connect=min(JINA_CONNECT_TIMEOUT, remaining_seconds),
        read=min(JINA_READ_TIMEOUT, remaining_seconds),
        write=min(JINA_WRITE_TIMEOUT, remaining_seconds),
        pool=min(JINA_POOL_TIMEOUT, remaining_seconds),
    )


async def _fetch_jina(
    jina_url: str, headers: dict[str, str], timeout: httpx.Timeout
) -> httpx.Response:
    """执行单次读取；由调用方的总 deadline 包住客户端创建、请求与关闭。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(jina_url, headers=headers)
        response.raise_for_status()
        return response


def _wall_time() -> float:
    return time.time()


class TavilyKeyPool:
    """轮询分发 Tavily key；账户级失败的 key 冷却后自动回池，不影响其他 key。"""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self._next = 0
        # key 序号 -> (冷却截止时间，None 表示停用到重启；触发的上游状态码)
        self._blocked: dict[int, tuple[float | None, int]] = {}

    def __bool__(self) -> bool:
        return bool(self._keys)

    def candidates(self) -> list[tuple[int, str]]:
        """本次请求可用的 key，从轮询游标开始排列。"""
        now = _wall_time()
        total = len(self._keys)
        ordered = [(self._next + offset) % total for offset in range(total)] if total else []
        if total:
            self._next = (self._next + 1) % total
        available = []
        for index in ordered:
            blocked = self._blocked.get(index)
            if blocked is not None:
                until, _status = blocked
                if until is None or until > now:
                    continue
                del self._blocked[index]
            available.append((index, self._keys[index]))
        return available

    def mark_unavailable(self, index: int, upstream_status: int) -> None:
        cooldown = TAVILY_KEY_COOLDOWNS[upstream_status]
        until = None if cooldown is None else _wall_time() + cooldown
        self._blocked[index] = (until, upstream_status)
        logger.warning(
            "reader_tavily_key_unavailable key_index=%s upstream_status=%s "
            "available=%s total=%s",
            index + 1,
            upstream_status,
            self.available_count(),
            len(self._keys),
        )

    def mark_ok(self, index: int) -> None:
        self._blocked.pop(index, None)

    def available_count(self) -> int:
        now = _wall_time()
        return sum(
            1
            for index in range(len(self._keys))
            if index not in self._blocked
            or (
                self._blocked[index][0] is not None
                and self._blocked[index][0] <= now
            )
        )

    def health_failure(self) -> dict | None:
        """所有 key 都不可用时返回告警信息（只含序号和状态码，不含 key）。"""
        if not self._keys or self.available_count() > 0:
            return None
        return {
            "message": "Tavily 号池全部 key 不可用（密钥无效、额度用完或限流）",
            "total": len(self._keys),
            "available": 0,
            "key_statuses": {
                str(index + 1): status
                for index, (_until, status) in sorted(self._blocked.items())
            },
        }


_tavily_pool = TavilyKeyPool(TAVILY_API_KEYS)


async def _fetch_tavily(url: str, api_key: str, timeout: float) -> dict:
    """单次 Tavily Extract；返回该 URL 的结果，失败或无结果时返回空 dict。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            TAVILY_EXTRACT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"urls": [url], "extract_depth": "basic", "format": "markdown"},
        )
        response.raise_for_status()
        payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {}
    return results[0]


async def _read_with_tavily(url: str, domain: str) -> tuple[str, str | None] | None:
    """Tavily 读取成功返回 (正文, 标题)；任何失败都返回 None，交给 Jina 回退。

    账户级失败（密钥无效/额度用完/限流）换下一个 key 重试，这类失败返回很快；
    超时、5xx、无正文属于页面级失败，换 key 也无济于事，直接回退 Jina。
    """
    started_at = _monotonic()
    deadline = started_at + TAVILY_TIMEOUT
    reason = "no_available_key"
    upstream_status = None
    for index, api_key in _tavily_pool.candidates():
        remaining = deadline - _monotonic()
        if remaining <= 0:
            reason = "timeout"
            break
        try:
            result = await asyncio.wait_for(
                _fetch_tavily(url, api_key, remaining), timeout=remaining
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            reason = "timeout"
            break
        except httpx.HTTPStatusError as exc:
            upstream_status = exc.response.status_code
            if upstream_status in TAVILY_KEY_COOLDOWNS:
                reason = "upstream_auth"
                _tavily_pool.mark_unavailable(index, upstream_status)
                continue
            reason = "upstream_error"
            break
        except Exception:
            reason = "request_error"
            break
        _tavily_pool.mark_ok(index)
        content = result.get("raw_content")
        if isinstance(content, str) and content.strip():
            title = result.get("title")
            return content, title if isinstance(title, str) and title.strip() else None
        reason = "empty_content"
        break
    logger.warning(
        "reader_tavily_fallback domain=%s kind=%s upstream_status=%s duration_ms=%s",
        domain,
        reason,
        upstream_status,
        max(0, int((_monotonic() - started_at) * 1000)),
    )
    return None


def _extract_title(content: str, header_title: str | None) -> str | None:
    """优先上游给的标题 → "Title: xxx" 行 → 首个 # 标题。"""
    if header_title:
        return header_title
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Title: "):
            return stripped[7:].strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _error_detail(
    *,
    kind: str,
    message: str,
    retryable: bool,
    upstream_status: int | None,
    attempts: int,
    started_at: float,
) -> dict:
    return {
        "kind": kind,
        "message": message,
        "retryable": retryable,
        "upstream_status": upstream_status,
        "attempts": attempts,
        "duration_ms": max(0, int((_monotonic() - started_at) * 1000)),
    }


def _log_failure(domain: str, status_code: int, detail: dict) -> None:
    logger.warning(
        "reader_read_failed domain=%s kind=%s status=%s upstream_status=%s "
        "attempts=%s duration_ms=%s",
        domain,
        detail["kind"],
        status_code,
        detail["upstream_status"],
        detail["attempts"],
        detail["duration_ms"],
    )


def _raise_reader_error(*, status_code: int, domain: str, detail: dict) -> None:
    _log_failure(domain, status_code, detail)
    raise HTTPException(status_code=status_code, detail=detail) from None


def _set_account_failure(upstream_status: int | None, message: str) -> None:
    global _account_failure
    if upstream_status in ACCOUNT_FAILURE_STATUSES:
        _account_failure = {
            "upstream_status": upstream_status,
            "message": message,
            "since": _account_failure["since"]
            if _account_failure
            else time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


def _clear_account_failure() -> None:
    global _account_failure
    _account_failure = None


@app.get("/health")
async def health():
    tavily_failure = _tavily_pool.health_failure()
    if _account_failure or tavily_failure:
        content: dict = {"status": "degraded"}
        if _account_failure:
            content["jina_account"] = _account_failure
        if tavily_failure:
            content["tavily_account"] = tavily_failure
        return JSONResponse(status_code=503, content=content)
    return {"status": "ok"}


class _JinaOutcome:
    """Jina 读取的结果；超时被取消时也保留已用的尝试次数，供失败详情使用。"""

    def __init__(self) -> None:
        self.attempts = 0
        self.response: httpx.Response | None = None
        self.error_status = 504
        self.error_kind = "timeout"
        self.error_message = "Jina Reader 请求超时"
        self.error_retryable = True
        self.upstream_status: int | None = None


async def _read_with_jina(url: str, deadline: float, outcome: _JinaOutcome) -> None:
    """读取成功时写入 outcome.response；失败时写入错误信息，可重试的错误在时限内重试一次。"""
    jina_url = f"{JINA_BASE_URL}/{url}"
    headers = {"Accept": "text/markdown"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    while outcome.attempts < JINA_MAX_ATTEMPTS:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return
        outcome.attempts += 1
        try:
            resp = await asyncio.wait_for(
                _fetch_jina(jina_url, headers, _timeout_for(remaining)),
                timeout=remaining,
            )
            if _monotonic() > deadline:
                raise asyncio.TimeoutError
            outcome.response = resp
            return
        except (httpx.TimeoutException, asyncio.TimeoutError):
            outcome.error_status = 504
            outcome.error_kind = "timeout"
            outcome.error_message = "Jina Reader 请求超时"
            outcome.error_retryable = True
            outcome.upstream_status = None
        except httpx.HTTPStatusError as exc:
            upstream_status = exc.response.status_code
            outcome.upstream_status = upstream_status
            outcome.error_status = 502
            if upstream_status in (401, 403):
                outcome.error_kind = "upstream_auth"
                outcome.error_message = "Jina Reader 上游鉴权失败"
                outcome.error_retryable = False
            elif upstream_status == 402:
                outcome.error_kind = "upstream_auth"
                outcome.error_message = "Jina Reader 账户余额不足"
                outcome.error_retryable = False
            elif upstream_status == 429:
                outcome.error_status = 503
                outcome.error_kind = "rate_limited"
                outcome.error_message = "Jina Reader 上游限流"
                outcome.error_retryable = True
            else:
                outcome.error_kind = "upstream_error"
                outcome.error_message = f"Jina Reader 上游返回 HTTP {upstream_status}"
                outcome.error_retryable = upstream_status >= 500
        except httpx.RequestError:
            outcome.error_status = 502
            outcome.error_kind = "request_error"
            outcome.error_message = "Jina Reader 请求失败"
            outcome.error_retryable = True
            outcome.upstream_status = None
        except Exception:
            outcome.error_status = 502
            outcome.error_kind = "request_error"
            outcome.error_message = "Jina Reader 请求失败"
            outcome.error_retryable = False
            outcome.upstream_status = None

        remaining = deadline - _monotonic()
        if not (
            outcome.error_retryable
            and outcome.attempts < JINA_MAX_ATTEMPTS
            and remaining - JINA_RETRY_BACKOFF >= JINA_MIN_RETRY_WINDOW
        ):
            _set_account_failure(outcome.upstream_status, outcome.error_message)
            return
        await asyncio.sleep(JINA_RETRY_BACKOFF)


async def _cancel(tasks: set[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@app.get("/read", response_model=ReadResponse)
async def read_url(
    url: str = Query(..., description="要读取的网页 URL"),
    full: Annotated[bool, Query(description="跳过正文提取，返回整页 Markdown")] = False,
):
    # 校验 URL 格式
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise HTTPException(status_code=422, detail="URL 格式无效") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not hostname:
        raise HTTPException(status_code=422, detail="URL 格式无效")

    start = _monotonic()
    deadline = start + READ_TOTAL_TIMEOUT
    domain = hostname
    favicon = f"{GOOGLE_FAVICON_API}{parsed.netloc}"

    def respond(content: str, title: str | None, provider: str, attempts: int) -> ReadResponse:
        fetch_ms = int((_monotonic() - start) * 1000)
        logger.info(
            "reader_read_succeeded domain=%s provider=%s status=200 "
            "attempts=%s duration_ms=%s",
            domain,
            provider,
            attempts,
            fetch_ms,
        )
        return ReadResponse(
            url=url,
            title=_extract_title(content, title),
            content=content,
            favicon=favicon,
            content_length=len(content),
            fetch_ms=fetch_ms,
            attempts=attempts,
            provider=provider,
        )

    # 直接抓取只给正文，导航不会占满调用方的正文窗口；表格、列表类页面可能提取不全，
    # 调用方需要整页时传 full=true 跳过它。
    direct_task = None if full else asyncio.create_task(read_direct(url, domain))
    tavily_task = None
    tavily_attempts = 0
    jina = _JinaOutcome()
    pending = {direct_task} if direct_task is not None else set()
    try:
        if direct_task is not None:
            await asyncio.wait(pending, timeout=DIRECT_HEDGE_DELAY)
            if direct_task.done():
                pending.discard(direct_task)
                if (page := direct_task.result()) is not None:
                    return respond(page.content, page.title, "direct", 1)

        if _tavily_pool:
            tavily_task = asyncio.create_task(_read_with_tavily(url, domain))
            tavily_attempts = 1
            pending.add(tavily_task)
        jina_task = asyncio.create_task(_read_with_jina(url, deadline, jina))
        pending.add(jina_task)

        while pending:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if direct_task in done and (page := direct_task.result()) is not None:
                return respond(page.content, page.title, "direct", 1)
            if tavily_task in done and (extracted := tavily_task.result()) is not None:
                return respond(extracted[0], extracted[1], "tavily", tavily_attempts)
            if jina_task in done:
                jina_task.result()
                if jina.response is not None:
                    _clear_account_failure()
                    return respond(
                        jina.response.text,
                        jina.response.headers.get("x-title"),
                        "jina",
                        jina.attempts + tavily_attempts,
                    )
    finally:
        await _cancel(pending)

    # 都没读到：Jina 已结束就报它的错误，仍在读则按超时处理。
    detail = _error_detail(
        kind=jina.error_kind,
        message=jina.error_message,
        retryable=jina.error_retryable,
        upstream_status=jina.upstream_status,
        attempts=jina.attempts + tavily_attempts,
        started_at=start,
    )
    _raise_reader_error(status_code=jina.error_status, domain=domain, detail=detail)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091, access_log=False)
