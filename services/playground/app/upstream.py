import json
from contextlib import suppress

import httpx

MAX_RESPONSE = 2 * 1024 * 1024


class UpstreamError(Exception):
    def __init__(self, code="upstream_error", status=502, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after


def text_result(text):
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"items": parsed}
    except json.JSONDecodeError:
        return {"content": text}


async def request(client, path, payload, headers, *, method="POST"):
    async with client.stream(method, path, json=payload, headers=headers) as response:
        if response.is_error:
            retry = response.headers.get("retry-after", "")
            retry = str(min(int(retry), 86400)) if retry.isdigit() else None
            code = {429: "rate_limited", 401: "credential_unavailable", 403: "service_not_allowed"}.get(
                response.status_code, "upstream_error"
            )
            if response.status_code == 429:
                # 429 也可能只是并发或频率限制；仅依据明确错误码判断额度用尽。
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 16384:
                        break
                    body.extend(chunk)
                with suppress(ValueError, TypeError, AttributeError):
                    failure = json.loads(body).get("error")
                    name = failure.get("code") if isinstance(failure, dict) else failure
                    if name in {"daily_limit_exceeded", "daily_quota_exceeded", "quota_exhausted"}:
                        code = "quota_exhausted"
            raise UpstreamError(code, 429 if response.status_code == 429 else 502, retry)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise UpstreamError("response_too_large")
        if method == "DELETE" or not body.strip():
            return None, response.headers
        text = body.decode("utf-8")
        if "text/event-stream" in response.headers.get("content-type", ""):
            # 忽略 SSE 注释、通知与心跳，只接收当前 JSON-RPC 请求的结果；支持多行 data。
            reply = None
            for event in text.replace("\r\n", "\n").split("\n\n"):
                lines = [line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")]
                if not lines:
                    continue
                item = json.loads("\n".join(lines))
                if isinstance(item, dict) and item.get("id") == payload.get("id"):
                    reply = item
            if reply is None:
                raise UpstreamError("invalid_response")
        else:
            reply = json.loads(text)
        if not isinstance(reply, dict):
            raise UpstreamError("invalid_response")
        return reply, response.headers


async def call_tool(client, name, tool, arguments, identity):
    headers = {"X-Caller-User": identity, "X-Caller-Scene": "playground"}
    if tool.get("path"):
        data, _ = await request(client, tool["path"], arguments, headers)
        if not data or not data.get("image_url"):
            raise UpstreamError("invalid_response")
        return data
    path = f"/{tool['service']}/mcp"
    headers["Accept"] = "application/json, text/event-stream"
    init, response_headers = await request(
        client,
        path,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agent-tools-playground", "version": "0.1.0"},
            },
        },
        headers,
    )
    if not init or not isinstance(init.get("result"), dict) or "error" in init:
        raise UpstreamError("invalid_response")
    headers["MCP-Protocol-Version"] = init["result"].get("protocolVersion", "2025-03-26")
    if session := response_headers.get("mcp-session-id"):
        headers["Mcp-Session-Id"] = session
    try:
        await request(client, path, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
        reply, _ = await request(
            client,
            path,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            headers,
        )
        if not reply or "error" in reply:
            raise UpstreamError("tool_error")
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise UpstreamError("tool_error")
        data = result.get("structuredContent")
        # FastMCP 会把返回字符串的工具包装成 {"result": "JSON 或正文"}。
        # 必须先解开这一层，再校验具体工具的数据契约。
        if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], str):
            data = text_result(data["result"])
        if not isinstance(data, dict) or not data:
            content = result.get("content", [])
            if not isinstance(content, list):
                raise UpstreamError("invalid_response")
            text = "\n".join(
                part["text"]
                for part in content
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
            )
            if not text.strip():
                raise UpstreamError("invalid_response")
            data = text_result(text)
        if data.get("ok") is not False and data.get("success") is not False:
            required = {
                "search_web": ("results", list),
                "read_page": ("content", str),
                "get_weather": ("forecast_days", list),
                "place_search": ("places", list),
                "route_compare": ("routes", list),
                "get_latest_news": ("data", list),
                "search_flights": ("services", list),
                "search_trains": ("services", list),
                "resolve-library-id": ("content", str),
                "query-docs": ("content", str),
            }
            key, expected = required[name]
            if not isinstance(data.get(key), expected) or (expected is str and not data[key].strip()):
                raise UpstreamError("invalid_response")
        return data
    finally:
        if session:
            # 每次试用独立会话；不把其他访客的 MCP 会话共享出去。
            with suppress(httpx.HTTPError, UpstreamError):
                async with client.stream("DELETE", path, headers=headers, timeout=3):
                    pass
