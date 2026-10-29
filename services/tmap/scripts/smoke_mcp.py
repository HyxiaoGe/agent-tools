"""部署冒烟：在 tmap-mcp 容器内走一遍 MCP 握手，确认白名单生效、能用真实密钥调通腾讯地图。

用法：docker exec -i tmap-mcp python - < scripts/smoke_mcp.py   （消耗一次 placeSuggestion 配额，6000 次/天）
"""

import json
import sys
import urllib.request

URL = "http://localhost:3336/mcp"
REQUIRED_TOOLS = {"placeSuggestion", "placeSearchNearby", "directionDriving", "matrix"}
FORBIDDEN_TOOLS = {"geocoder", "weather", "ipLocation"}


def post(payload, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
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
    missing = REQUIRED_TOOLS - names
    if missing:
        sys.exit(f"missing tools: {sorted(missing)}")
    leaked = sorted(names & FORBIDDEN_TOOLS)
    if leaked:
        sys.exit(f"excluded tools exposed: {leaked}")

    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "placeSuggestion", "arguments": {"keyword": "天安门", "region": "北京"}},
    }
    result, _ = post(call, session)
    text = "".join(item.get("text", "") for item in result["result"].get("content", []))
    # 控制台没给 key 分配额度时上游不报错、只回「未找到结果」，所以要看到坐标才算调通。
    if result["result"].get("isError") or "经度" not in text:
        sys.exit(f"placeSuggestion failed: {text[:300]}")
    print(f"ok: {len(names)} tools, placeSuggestion returned {len(text)} chars")


main()
