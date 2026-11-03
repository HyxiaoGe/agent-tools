"""部署冒烟：在 weather-service 容器内用真实凭证查一次，确认凭证有效、能连通和风，REST 和 MCP 都能用。

REST 查“北京”和它的逐日预报（两次和风请求）；MCP 列工具，get_weather 查“北京”（城市、逐日、逐小时三次），
再带明天的日期查一次（全走缓存，不再请求和风）。共消耗五次和风请求。

用法：docker exec -i weather-service python - < scripts/smoke_weather.py
"""

import json
import sys
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

BASE = "http://localhost:8092"


def get(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


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


def get_weather(**arguments):
    result = rpc("tools/call", {"name": "get_weather", "arguments": arguments})
    weather = result.get("structuredContent") or {}
    if result.get("isError") or not weather.get("ok"):
        sys.exit(f"get_weather failed: {json.dumps(weather, ensure_ascii=False)[:300]}")
    return weather


def main():
    health = get("/health")
    if not health.get("configured"):
        sys.exit("weather-service has no QWeather credentials configured")
    cities = get("/v1/cities", location="北京", number=1)["cities"]
    if not cities or cities[0]["name"] != "北京":
        sys.exit(f"unexpected city lookup result: {json.dumps(cities, ensure_ascii=False)[:300]}")
    daily = get("/v1/forecast/daily", location_id=cities[0]["location_id"])["daily"]
    if len(daily) < 3 or not daily[0].get("text_day"):
        sys.exit(f"unexpected daily forecast: {json.dumps(daily, ensure_ascii=False)[:300]}")
    print(f"rest ok: {cities[0]['adm1']}·{cities[0]['name']} ({cities[0]['location_id']}), {len(daily)} days")

    tools = [tool["name"] for tool in rpc("tools/list")["tools"]]
    if tools != ["get_weather"]:
        sys.exit(f"unexpected MCP tools: {tools}")
    weather = get_weather(location="北京")
    # 北京的当地今天就是北京时间的今天（容器是 UTC，不能用 date.today()）
    beijing_today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if weather["local_today"] != beijing_today or weather["forecast_days"][0]["date"] != beijing_today:
        sys.exit(f"unexpected local today: {weather['local_today']} vs {beijing_today}")
    if weather["day_count"] != 4 or "hourly_summary" not in weather:
        sys.exit(f"unexpected forecast: {json.dumps(weather, ensure_ascii=False)[:300]}")
    tomorrow = (date.fromisoformat(beijing_today) + timedelta(days=1)).isoformat()
    dated = get_weather(location="北京", requested_date=tomorrow)
    if [day["date"] for day in dated["forecast_days"]] != [tomorrow] or dated["fetched_at"] != weather["fetched_at"]:
        sys.exit(f"unexpected dated forecast: {json.dumps(dated, ensure_ascii=False)[:300]}")
    print(f"mcp ok: {weather['resolved_location']}, today {beijing_today}, {weather['limitations']}")


main()
