import asyncio
import json
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import nh3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.catalog import TOOLS, catalog
from app.examples import RESULTS as EXAMPLE_RESULTS
from app.limits import Limiter, LimitError
from app.settings import Settings
from app.upstream import UpstreamError, call_tool

STATIC = Path(__file__).parent / "static"
IMAGE_FILE = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.(?:png|jpeg|jpg|webp)")
MAX_IMAGE = 20 * 1024 * 1024
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")
MESSAGES = {
    "not_configured": "在线试用尚未配置，请先为页面服务配置服务端产品密钥。",
    "quota_unavailable": "试用计数暂时不可用，为保护共享额度，已暂停调用，请稍后重试。",
    "quota_exhausted": "该工具的可用额度已用完，请稍后再试。",
    "rate_limited": "这次调用受到频率或并发限制，请稍后重试。",
    "credential_unavailable": "试用凭证暂不可用，请联系站点维护者。",
    "service_not_allowed": "试用账号尚未获准调用此服务。",
    "timeout": "这次调用等待超时，请稍后再试。",
    "response_too_large": "返回内容超过试用页面的大小限制。",
    "invalid_response": "工具未返回可展示的结果。",
    "tool_error": "工具未能完成这次查询，请检查参数或更换来源。",
    "upstream_error": "工具服务暂时不可用，请稍后再试。",
    "unsafe_url": "这个地址无法安全读取，请提供公开网页的地址。",
    "page_not_found": "原网页已不存在，请选择其他搜索来源。",
    "page_error": "原网页拒绝访问或返回了错误，请打开原网页核对。",
    "no_content": "工具没有提取到可用正文，请更换来源或尝试保留整页内容。",
    "ambiguous_location": "找到了多个同名地点，请补充省、市或区后再查。",
    "place_not_found": "没有找到这个地点，请补充城市或更换名称。",
    "no_route": "没有找到可用路线，请检查起终点或更换出行方式。",
    "no_services": "没有找到符合条件的直达班次，请调整日期或城市。",
    "origin_not_recognized": "供应商未识别出发地点，请使用完整城市、机场或车站名称。",
    "destination_not_recognized": "供应商未识别到达地点，请使用完整城市、机场或车站名称。",
    "requested_date_not_covered": "所选日期超出预报范围。",
}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(max_length=80)
    arguments: dict = Field(default_factory=dict)


def error(code, status, message=None, **extra):
    return JSONResponse({"ok": False, "error": {"code": code, "message": message or MESSAGES[code]}, **extra}, status)


def render_markdown(text):
    # 网页正文属于不可信输入：禁用原始 HTML，清理危险链接，不自动加载文章里的远程图片。
    return nh3.clean(
        MARKDOWN.render(text),
        tags={
            "p",
            "br",
            "hr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
            "em",
            "strong",
            "a",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
        },
        attributes={"a": {"href", "title"}, "code": {"class"}, "ol": {"start"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )


def create_app(config=None, *, transport=None):
    settings = config or Settings()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=settings.gateway_url,
            headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
            timeout=httpx.Timeout(160, connect=5),
            limits=httpx.Limits(max_connections=settings.max_concurrent + 2),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            app.state.gateway = client
            app.state.limiter = Limiter(settings)
            yield

    app = FastAPI(title="agent-tools playground", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/images/{filename}")
    async def get_image(filename: str, request: Request):
        # 只读取现有生图服务的固定静态目录，不接受任意地址，不触发生图或占用试用次数。
        if not IMAGE_FILE.fullmatch(filename):
            return error("page_not_found", 404)
        if not settings.configured:
            return error("not_configured", 503)
        try:
            async with request.app.state.gateway.stream(
                "GET", f"/image/static/images/{filename}", timeout=20
            ) as upstream:
                if upstream.status_code != 200:
                    return error("page_not_found", 404) if upstream.status_code == 404 else error("upstream_error", 502)
                mime = upstream.headers.get("content-type", "").split(";", 1)[0].lower()
                if mime not in {"image/png", "image/jpeg", "image/webp"}:
                    return error("invalid_response", 502)
                body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(body) + len(chunk) > MAX_IMAGE:
                        return error("response_too_large", 502)
                    body.extend(chunk)
                return Response(bytes(body), media_type=mime)
        except httpx.TimeoutException:
            return error("timeout", 504)
        except httpx.HTTPError:
            return error("upstream_error", 502)

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http:; "
            "connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "configured": settings.configured}

    @app.get("/api/catalog")
    async def get_catalog():
        return {
            **catalog(settings.configured),
            "limits": {"requests_per_day": settings.requests_per_day, "images_per_day": settings.images_per_day},
        }

    @app.get("/api/examples")
    async def get_examples():
        samples = {}
        for tool in catalog(settings.configured)["tools"]:
            result = EXAMPLE_RESULTS[tool["id"]]
            sample = {
                "ok": True,
                "example": True,
                "tool": tool["id"],
                "label": tool["examples"][0]["label"],
                "arguments": {name: value for name, value in tool["examples"][0]["arguments"].items() if value != ""},
                "result": result,
            }
            if tool["id"] == "read_page":
                sample["label"] = "Python asyncio 正文"
                sample["arguments"]["url"] = result["url"]
            if isinstance(result.get("content"), str):
                sample["rendered_html"] = render_markdown(result["content"])
            samples[tool["id"]] = sample
        return {"samples": samples}

    def identity_for(request):
        return request.app.state.limiter.identity(request.client.host if request.client else "unknown")

    async def quota_for(request, identity):
        return await asyncio.to_thread(request.app.state.limiter.counter.snapshot, identity)

    async def with_quota(request, response, identity):
        try:
            quota = await quota_for(request, identity)
        except sqlite3.Error:
            return error("quota_unavailable", 503)
        payload = json.loads(response.body)
        payload["quota"] = quota
        headers = {"Retry-After": response.headers["retry-after"]} if "retry-after" in response.headers else None
        return JSONResponse(payload, response.status_code, headers=headers)

    @app.get("/api/quota")
    async def get_quota(request: Request):
        try:
            return await quota_for(request, identity_for(request))
        except sqlite3.Error:
            return error("quota_unavailable", 503)

    @app.post("/api/run")
    async def run(request: Request):
        origin = request.headers.get("origin")
        expected = settings.public_origin or str(request.base_url).rstrip("/")
        if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            return error("cross_origin", 403, "请从本站页面发起试用。")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return error("invalid_request", 415, "请求需要使用 JSON 格式。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return error("payload_too_large", 413, "输入内容过长。")
        try:
            payload = RunRequest.model_validate_json(body)
            tool = TOOLS.get(payload.tool)
            if tool is None:
                return error("unknown_tool", 422, "这个工具未开放在线试用。")
            arguments = tool["model"].model_validate(payload.arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            fields = [".".join(map(str, issue["loc"])) for issue in exc.errors()]
            return error("invalid_arguments", 422, "请检查输入内容与参数范围。", fields=fields)
        if not settings.configured:
            return error("not_configured", 503)
        limiter = request.app.state.limiter
        # 只使用服务端识别的地址；不信任浏览器提交的 X-Forwarded-For 或身份头。
        identity = identity_for(request)
        started = time.monotonic()
        try:
            async with limiter.admit(identity, image=payload.tool == "generate_image"):
                async with asyncio.timeout(165 if payload.tool == "generate_image" else 100):
                    result = await call_tool(request.app.state.gateway, payload.tool, tool, arguments, identity)
            # 防止上游异常回显产品密钥，调用详情也只返回经过处理的业务结果。
            result = json.loads(
                json.dumps(result, ensure_ascii=False).replace(settings.api_key.get_secret_value(), "[已隐藏]")
            )
            duration = round((time.monotonic() - started) * 1000)
            failed = result.get("ok") is False or result.get("success") is False
            envelope = {
                "ok": not failed,
                "tool": payload.tool,
                "arguments": arguments,
                "result": result,
                "duration_ms": duration,
            }
            if failed:
                upstream_error = result.get("error")
                code = upstream_error.get("code", "tool_error") if isinstance(upstream_error, dict) else "tool_error"
                envelope["error"] = {
                    "code": code,
                    "message": MESSAGES.get(code, "这次查询没有得到可用结果，请查看返回说明或调整参数。"),
                }
            if isinstance(result.get("content"), str):
                envelope["rendered_html"] = render_markdown(result["content"])
            return await with_quota(request, JSONResponse(envelope, 422 if failed else 200), identity)
        except LimitError as exc:
            response = error(exc.code, 429, exc.message)
            response.headers["Retry-After"] = str(exc.retry_after)
            return await with_quota(request, response, identity)
        except UpstreamError as exc:
            response = error(exc.code, exc.status)
            if exc.retry_after:
                response.headers["Retry-After"] = exc.retry_after
            return await with_quota(request, response, identity)
        except sqlite3.Error:
            return error("quota_unavailable", 503)
        except (TimeoutError, httpx.TimeoutException):
            return await with_quota(request, error("timeout", 504), identity)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return await with_quota(request, error("upstream_error", 502), identity)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app
