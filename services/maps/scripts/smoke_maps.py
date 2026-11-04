"""部署冒烟：在 maps-service 容器内用真实 key 查几次，确认两把 key 都有效、MCP 两个工具能用。

MCP 列工具；place_search 在上海搜一次咖啡（高德 1 次）；route_compare 从虹桥火车站到搜到的那家，
按编号定终点，比较驾车和公交（高德地址解析、详情、驾车、公交共 4 次）。
腾讯不经 MCP（高德好着就不会转过去），直接用服务里的客户端查一次输入提示（腾讯 1 次）。
共消耗高德 5 次、腾讯 1 次，都在免费额度里。

用法：docker exec -i maps-service python - < scripts/smoke_maps.py
"""

import asyncio
import json
import sys
import urllib.request

BASE = "http://localhost:8094"


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as response:
        return json.loads(response.read().decode())


def rpc(method, params=None):
    """无状态的 MCP：不用先 initialize，一个 POST 一个回答（SSE 里只有一条 data）。"""

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    request = urllib.request.Request(
        f"{BASE}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-caller-app": "smoke",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode()
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    reply = json.loads(text)
    if "result" not in reply:
        sys.exit(f"MCP {method} failed: {text[:300]}")
    return reply["result"]


def call(name, **arguments):
    result = rpc("tools/call", {"name": name, "arguments": arguments})
    data = result.get("structuredContent") or {}
    if result.get("isError") or not data.get("ok"):
        sys.exit(f"{name} failed: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data


async def tmap_suggestion():
    # 容器里的工作目录是 /app，python - 能直接 import 服务的代码
    import httpx

    from app.config import Settings
    from app.tmap import TmapClient
    from app.upstream import Gate, QuotaMemo, Usage

    key = Settings().tmap_api_key.get_secret_value()
    async with httpx.AsyncClient(timeout=10) as http:
        client = TmapClient(key, http, Gate("tmap", QuotaMemo(), Usage()))
        lookup = await client.geocode("外滩", "上海")
    return lookup


def main():
    health = get("/health")
    if health.get("providers") != ["amap", "tmap"]:
        sys.exit(f"maps-service providers: {health.get('providers')}")
    tools = sorted(tool["name"] for tool in rpc("tools/list")["tools"])
    if tools != ["place_search", "route_compare"]:
        sys.exit(f"unexpected MCP tools: {tools}")

    found = call("place_search", query="咖啡", anchor_source="none", city="上海", limit=1)
    (cafe,) = found["places"]
    if found["providers"] != ["amap"] or "src=smoke" not in cafe.get("map_url", ""):
        sys.exit(f"unexpected place: {json.dumps(found, ensure_ascii=False)[:300]}")
    print(f"place_search ok: {cafe['name']} ({cafe['place_id']})")

    routes = call(
        "route_compare",
        origin="虹桥火车站",
        origin_source="named",
        origin_city="上海",
        destination=cafe["name"],
        destination_source="named",
        destination_place_id=cafe["place_id"],
        modes=["driving", "transit"],
    )
    modes = [route["mode"] for route in routes["routes"]]
    if modes != ["driving", "transit"] or routes["providers"] != ["amap"]:
        sys.exit(f"unexpected routes: {json.dumps(routes, ensure_ascii=False)[:300]}")
    driving, transit = routes["routes"]
    print(f"route_compare ok: driving {driving['distance_m']} m / {driving['duration_s']} s")
    print(f"route_compare ok: transit {transit['duration_s']} s, {transit.get('transit_type')}")

    lookup = asyncio.run(tmap_suggestion())
    if len(lookup.candidates) != 1 or not lookup.places:
        sys.exit(f"unexpected tmap suggestion: {len(lookup.candidates)} candidates")
    print(f"tmap ok: {len(lookup.places)} suggestions")

    usage = get("/usage/daily")
    print(f"usage today: {json.dumps(usage['providers'], ensure_ascii=False)}, fallbacks {usage['fallbacks']}")


main()
