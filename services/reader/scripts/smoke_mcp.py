"""部署冒烟：在 reader-service 容器内经 MCP 读一次，确认无状态 /mcp 能用、网址检查和真实读取都正常。

tools/list 看工具在不在、参数对不对；一次内网地址（url_safety 拦下，不打上游）；一次真实读取 example.com
（和 REST 冒烟同一页。这页正文太短，直连会跳过，所以每次部署实际会占一次付费上游）。

用法：docker exec -i reader-service python - < scripts/smoke_mcp.py
"""

import json
import sys
import urllib.request

BASE = "http://localhost:8091"


def rpc(method, params=None):
    """无状态的 MCP：不用先 initialize，一个 POST 一个回答（SSE 里只有一条 data）。"""

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    request = urllib.request.Request(
        f"{BASE}/mcp",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode()
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    reply = json.loads(text)
    if "result" not in reply:
        sys.exit(f"MCP {method} failed: {text[:300]}")
    return reply["result"]


def read_page(**arguments):
    result = rpc("tools/call", {"name": "read_page", "arguments": arguments})
    if result.get("isError"):
        sys.exit(f"read_page raised: {json.dumps(result, ensure_ascii=False)[:300]}")
    return result.get("structuredContent") or {}, result["content"][0]["text"]


def main():
    tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
    if set(tools) != {"read_page"}:
        sys.exit(f"unexpected MCP tools: {sorted(tools)}")
    params = set(tools["read_page"]["inputSchema"]["properties"])
    if params != {"url", "full_page"}:
        sys.exit(f"unexpected read_page parameters: {sorted(params)}")

    blocked, _ = read_page(url="http://127.0.0.1:8091/health")
    if blocked.get("ok") is not False or blocked["error"]["code"] != "unsafe_url":
        sys.exit(f"private address not rejected: {json.dumps(blocked, ensure_ascii=False)[:300]}")

    data, text = read_page(url="https://example.com")
    if data.get("ok") is not True or not data.get("content", "").strip():
        sys.exit(f"real read failed: {json.dumps(data, ensure_ascii=False)[:300]}")
    if "Markdown Content:" in data["content"] or "URL Source:" in data["content"]:
        sys.exit("Jina wrapper was not removed")
    if "meta" in json.loads(text):
        sys.exit("model-visible text leaks meta")
    meta = data["meta"]
    print(f"mcp ok: {data['content_chars']} chars via {meta['provider']} ({meta['attempts']} attempts, {meta['fetch_ms']} ms)")


main()
