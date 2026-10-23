"""部署冒烟：在 trends-mcp 容器内走一遍 MCP 握手，确认工具齐全、能读到 TrendRadar 的数据，
且 contract_server.py 的契约生效（参数带可选值、上游出错标记为 isError）。

用法：docker exec -i trends-mcp python - < scripts/smoke_mcp.py
"""

import json
import sys
import urllib.request

URL = "http://localhost:3333/mcp"
# Fusion 授权使用的查询类工具，缺一个都说明镜像版本和预期不符。
REQUIRED_TOOLS = {
    "get_latest_news",
    "get_trending_topics",
    "search_news",
    "get_news_by_date",
    "list_available_dates",
}


def post(payload, session=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode()
        session = response.headers.get("mcp-session-id") or session
    if not body.strip():
        return None, session
    # 流式 HTTP 可能以 SSE 返回，取最后一条 data 行。
    if body.lstrip().startswith("{"):
        return json.loads(body), session
    data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    return json.loads(data[-1]), session


def main():
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1"},
        },
    }
    _, session = post(init)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

    listed, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    names = {tool["name"] for tool in listed["result"]["tools"]}
    missing = REQUIRED_TOOLS - names
    if missing:
        sys.exit(f"missing tools: {sorted(missing)}")

    schemas = {
        tool["name"]: tool["inputSchema"]["properties"]
        for tool in listed["result"]["tools"]
    }
    if "enum" not in schemas["search_news"]["sort_by"]:
        sys.exit("contract not applied: search_news.sort_by has no enum")

    def call(request_id, name, arguments):
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        result, _ = post(payload, session)
        text = "".join(
            item.get("text", "") for item in result["result"].get("content", [])
        )
        return bool(result["result"].get("isError")), text

    is_error, text = call(3, "get_latest_news", {"limit": 3})
    if is_error or not text:
        sys.exit(f"get_latest_news failed: {text[:300]}")
    # 参数合法但上游拒绝（超出 365 天）：必须标记为错误，而不是当成功返回。
    rejected, rejected_text = call(4, "get_news_by_date", {"date_range": "2020-01-01"})
    if not rejected:
        sys.exit(f"upstream error not marked isError: {rejected_text[:300]}")
    print(
        f"ok: {len(names)} tools, get_latest_news returned {len(text)} chars, upstream errors marked isError"
    )


main()
