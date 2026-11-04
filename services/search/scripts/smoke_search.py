"""部署冒烟：在 search-service 容器内经 MCP 查一次，确认无状态 /mcp 能用、search_web 的参数检查和真实搜索都正常。

tools/list 看工具在不在、参数对不对；一次不合法的参数（不打上游）；一次真实搜索（count=3，十分钟内重复部署走缓存）。
最多占主 provider 一个每日名额。今天额度已经用完时只提示、不算部署失败（那是运维状态，不是代码问题）。

用法：docker exec -i search-service python - < scripts/smoke_search.py
"""

import json
import sys
import urllib.request

BASE = "http://localhost:8080"


def rpc(method, params=None):
    """无状态的 MCP：不用先 initialize，一个 POST 一个回答（SSE 里只有一条 data）。"""

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    request = urllib.request.Request(
        f"{BASE}/mcp",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        text = response.read().decode()
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    reply = json.loads(text)
    if "result" not in reply:
        sys.exit(f"MCP {method} failed: {text[:300]}")
    return reply["result"]


def search_web(**arguments):
    result = rpc("tools/call", {"name": "search_web", "arguments": arguments})
    if result.get("isError"):
        sys.exit(f"search_web raised: {json.dumps(result, ensure_ascii=False)[:300]}")
    return result.get("structuredContent") or {}, result["content"][0]["text"]


def main():
    tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
    if not {"search_web", "search", "search_news", "search_images"} <= set(tools):
        sys.exit(f"unexpected MCP tools: {sorted(tools)}")
    params = set(tools["search_web"]["inputSchema"]["properties"])
    if params != {"query", "count", "recency_days", "domains"}:
        sys.exit(f"unexpected search_web parameters: {sorted(params)}")

    bad, _ = search_web(query="smoke", domains=["https://example.com/path"])
    if bad.get("ok") is not False or bad["error"]["code"] != "bad_arguments":
        sys.exit(f"bad arguments not rejected: {json.dumps(bad, ensure_ascii=False)[:300]}")

    data, text = search_web(query="Python programming language", count=3)
    if data.get("ok") is False and data["error"]["code"] == "quota_exhausted":
        print("search quota exhausted for today; skipped the real search check")
        return
    if data.get("ok") is not True or not data.get("results"):
        sys.exit(f"real search failed: {json.dumps(data, ensure_ascii=False)[:300]}")
    first = data["results"][0]
    if not first.get("url", "").startswith("http") or len(first.get("snippet", "")) > 1000:
        sys.exit(f"unexpected result shape: {json.dumps(first, ensure_ascii=False)[:300]}")
    if "meta" in json.loads(text):
        sys.exit("model-visible text leaks meta")
    meta = data["meta"]
    print(
        f"mcp ok: {len(data['results'])} results via {meta['provider']} (chain {meta['provider_chain']}, cached {meta['cached']})"
    )


main()
