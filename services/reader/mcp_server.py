"""MCP 工具 read_page（/mcp，无状态，和 weather、search 一样）：产品经网关 /reader/mcp 给自己的模型用。

读取本身和 REST /read 同一段（main.read_page 由 main 传进来，这里不 import main），这里多做三件事：
- 读之前查网址（url_safety）：不合法、指向内网、带凭据的不交给任何上游；
- 读到的原文变成给模型的正文（page_text）：剥 Jina 包装、认出没正文的、从文章标题起、截断一次；
- 失败统一成 {code, message, retryable}，选最能说明问题的原因（不只看 Jina 的）。

说明（英文，模型看的）Fusion 和灵稿共用，只写工具本身；什么时候读、读哪些由产品接在后面。
结果：structuredContent 是整份（meta 里是哪家读到的、试了几次、用时，给日志）；文字是去掉 meta 的 JSON（给模型）。
失败：structuredContent 是 {ok:false, error:{code, message, retryable}}，isError 保持 false，和 weather、search 一样。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

import url_safety
from page_text import MAX_CHARS, NoContent, page_text

logger = logging.getLogger("reader_service")

TOOL_NAME = "read_page"
RETRYABLE_PAGE_STATUSES = {408, 425, 429}

DESCRIPTION = f"""Read one web page and get its text as Markdown, with its title.

By default the main text is extracted and navigation and other page parts are left out. Set full_page=true to read the whole page when an extracted read is missing tables, lists, or data you need.

The result is JSON:
- url and title of the page.
- content: the page text, at most {MAX_CHARS} characters.
- content_chars: the length of the page text before truncation.
- truncated, only when true: content was cut and only the beginning is shown.
- extracted, only when true: the main text was extracted automatically, so tables, lists, or other parts may be missing; read again with full_page=true if you need them.
- note: what truncated or extracted means for this page, when either applies.

On failure the result has ok=false and an error with code, message, and retryable, and says what to do next. A page that was not read cannot be used as evidence; do not guess or fabricate its content."""

URL_DESCRIPTION = "The complete http:// or https:// URL to read."
FULL_PAGE_DESCRIPTION = (
    "Optional, default false. Set to true to read the whole page instead of the extracted main text, when an "
    "extracted read is missing tables, lists, or data you need."
)

MESSAGES = {
    "invalid_url": "The URL is invalid.",
    "unsafe_url": "The URL cannot be read.",
    "page_not_found": "The page does not exist.",
    "page_error": "The site returned an error page.",
    "access_blocked": "The site blocked automated reading.",
    "empty_content": "No readable text was found on the page.",
    "timeout": "The page did not load in time.",
    "rate_limited": "The page-reading service was rate-limited.",
    "quota_exhausted": "The page-reading service has used up its quota for today.",
    "upstream_error": "The page-reading service returned an error.",
}
NOT_EVIDENCE = " The page cannot be used as evidence; do not fabricate its content."


def _model_text(code: str, message: str, retryable: bool, status: int | None) -> str:
    if code == "invalid_url":
        return f"Not read: {message}. Check the URL."
    if code == "unsafe_url":
        return f"Not read: {message}. Do not try to read this URL." + NOT_EVIDENCE
    if code == "page_not_found":
        return (
            f"The page does not exist (the site returned HTTP {status or 404}): the link is dead or the address is "
            "wrong. Do not read this URL again; find another source if the information is still needed." + NOT_EVIDENCE
        )
    if code == "access_blocked":
        return (
            "The site blocked automated reading (for example with a CAPTCHA or a login wall), so no page text was "
            "obtained. Do not retry this URL; use another source." + NOT_EVIDENCE
        )
    if code == "empty_content":
        return "No readable text was found on this page. Do not retry this URL; use another source." + NOT_EVIDENCE
    if code == "quota_exhausted":
        return (
            "Not read: the page-reading service has used up its quota for today, so other pages that need it will "
            "fail the same way. Do not keep trying other pages to work around it; continue with search results and "
            "pages already read, and say what could not be checked." + NOT_EVIDENCE
        )
    if code == "page_error":
        head = f"The site returned HTTP {status} instead of the page."
    else:
        head = MESSAGES[code]
    if retryable:
        return f"{head} You may retry once; if it fails again, use another source." + NOT_EVIDENCE
    return f"{head} Do not retry this URL; use another source." + NOT_EVIDENCE


def failure(code: str, message: str | None = None, *, retryable: bool = False, status: int | None = None) -> ToolResult:
    text = message or MESSAGES[code]
    error: dict[str, Any] = {"code": code, "message": text, "retryable": retryable}
    if status is not None:
        error["status"] = status
    return ToolResult(
        content=_model_text(code, text, retryable, status), structured_content={"ok": False, "error": error}
    )


def _page_failure(status: int) -> ToolResult:
    """原网页返回的错误状态码：404/410 不存在，401/403 拦了，其余看能不能重试。"""
    if status in (404, 410):
        return failure("page_not_found", status=status)
    if status in (401, 403):
        return failure("access_blocked", status=status)
    return failure("page_error", retryable=status in RETRYABLE_PAGE_STATUSES or status >= 500, status=status)


def classify(exc: HTTPException) -> ToolResult:
    """REST 那套 detail 换成 MCP 的错误。全部失败时 REST 只报 Jina 的原因；Firecrawl 看到原网页报错
    （page_status）而 Jina 只是超时、出错时，原网页的错误更能说明问题。"""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    kind = detail.get("kind")
    upstream_status = detail.get("upstream_status")
    page_status = getattr(exc, "page_status", None)
    if kind in ("page_not_found", "page_error") and isinstance(upstream_status, int):
        return _page_failure(upstream_status)
    if kind == "daily_quota_exceeded":
        return failure("quota_exhausted")
    if isinstance(page_status, int):
        return _page_failure(page_status)
    if kind == "timeout":
        return failure("timeout", retryable=True)
    if kind == "rate_limited":
        return failure("rate_limited", retryable=True)
    return failure("upstream_error", retryable=detail.get("retryable") is True)


def success(url: str, response: Any) -> ToolResult:
    try:
        text = page_text(response.content, response.title)
    except NoContent as exc:
        logger.warning("reader_mcp_no_content kind=%s provider=%s", exc.code, response.provider)
        return failure(exc.code)
    result: dict[str, Any] = {"ok": True, "url": url, "title": response.title or "", "content": text.content}
    if text.truncated:
        result["truncated"] = True
    result["content_chars"] = text.total_chars
    notes = []
    if text.truncated:
        notes.append(f"Content truncated: showing the first {MAX_CHARS} of {text.total_chars} characters.")
    if response.provider == "direct":
        result["extracted"] = True
        notes.append(
            "Main text was extracted automatically; tables, lists, or other parts may be missing. "
            "Read again with full_page=true if you need them."
        )
    if notes:
        result["note"] = " ".join(notes)
    result["meta"] = {"provider": response.provider, "attempts": response.attempts, "fetch_ms": response.fetch_ms}
    seen = {k: v for k, v in result.items() if k != "meta"}
    return ToolResult(content=json.dumps(seen, ensure_ascii=False), structured_content=result)


async def read_page_tool(read: Callable[[str, bool], Awaitable[Any]], url: str, full_page: bool) -> ToolResult:
    try:
        target = url_safety.check_url(url)
    except url_safety.UrlRejected as exc:
        return failure(exc.code, exc.message)
    host = (urlsplit(target).hostname or "").lower().rstrip(".")
    if await url_safety.resolves_to_private(host):
        logger.warning("reader_mcp_unsafe_url domain=%s kind=private_dns", host)
        return failure("unsafe_url", "the URL's host resolves to a private address")
    try:
        response = await read(target, full_page)
    except HTTPException as exc:  # main.ReadFailed；失败已在 main 里记过日志
        return classify(exc)
    return success(target, response)


def build_mcp(read: Callable[[str, bool], Awaitable[Any]]) -> FastMCP:
    mcp = FastMCP("reader", instructions="read_page reads one web page; see the tool description.")

    @mcp.tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
        output_schema=None,
    )
    async def read_page(
        url: Annotated[str, Field(description=URL_DESCRIPTION)],
        full_page: Annotated[bool, Field(description=FULL_PAGE_DESCRIPTION)] = False,
    ) -> ToolResult:
        return await read_page_tool(read, url, full_page)

    return mcp
