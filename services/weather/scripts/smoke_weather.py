"""部署冒烟：在 weather-service 容器内用真实凭证查一次城市和逐日预报，确认凭证有效、能连通和风。

用法：docker exec -i weather-service python - < scripts/smoke_weather.py   （消耗两次和风请求）
"""

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://localhost:8092"


def get(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


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
    print(f"ok: {cities[0]['adm1']}·{cities[0]['name']} ({cities[0]['location_id']}), {len(daily)} days")


main()
