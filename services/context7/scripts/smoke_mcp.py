"""部署冒烟：在 context7-mcp 容器内走一遍 MCP 握手，确认白名单生效、能用真实密钥调通 Context7。

用法：docker exec -i context7-mcp python - < scripts/smoke_mcp.py   （消耗一次 resolve-library-id 请求）
"""

import json
import sys
import urllib.request

URL = "http://localhost:3335/mcp"
REQUIRED_TOOLS = {"resolve-library-id", "query-docs"}


def post(payload, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode()
        session = response.headers.get("mcp-session-id") or session
    if not body.strip():
        return None, session
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
    if names != REQUIRED_TOOLS:
        sys.exit(f"unexpected tools: {sorted(names)}")

    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "resolve-library-id",
            "arguments": {"libraryName": "FastAPI", "query": "how to configure lifespan events"},
        },
    }
    result, _ = post(call, session)
    text = "".join(item.get("text", "") for item in result["result"].get("content", []))
    if result["result"].get("isError") or "library ID" not in text:
        sys.exit(f"resolve-library-id failed: {text[:300]}")
    print(f"ok: {len(names)} tools, resolve-library-id returned {len(text)} chars")


main()
