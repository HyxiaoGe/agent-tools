"""仅供本地浏览器自动化的网关替身，不被应用导入，不访问供应商。

启动：uv run uvicorn tests.fake_gateway:app --host 127.0.0.1 --port 18100
试用页面另用测试密钥 test-only、该网关地址在 18101 启动。
"""

import asyncio
import base64
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()

ARTICLE = """# Python 异步编程指南（自动化测试资料）

这段正文来自本地测试网关，用于验证搜索后读取的完整交互，不是供应商返回的数据。

## 从协程开始

使用 **async / await** 编写并发任务，等待网络请求时允许其他任务继续运行。

```python
async def main():
    results = await asyncio.gather(fetch_a(), fetch_b())
```

| 场景 | 建议 |
| --- | --- |
| 多个独立请求 | 并发执行 |
| 有前后依赖 | 按序执行 |

[Python 官方文档](https://docs.python.org/3/library/asyncio.html)

<script>window.articleExecuted = true</script>
"""


def tool_result(name, args):
    if name == "search_web":
        if args["query"] == "失败":
            return {"ok": False, "error": {"code": "quota_exhausted"}}
        return {
            "ok": True,
            "query": args["query"],
            "results": [
                {
                    "title": "Python asyncio 官方文档（测试）",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "snippet": "了解协程、任务与事件循环。这是供自动化检查使用的搜索样本。",
                },
                {
                    "title": "无法读取的来源（测试）",
                    "url": "https://example.com/failure",
                    "snippet": "用于验证正文读取失败后，搜索列表仍然保留。",
                },
                {
                    "title": "较慢的来源（测试）",
                    "url": "https://example.com/slow",
                    "snippet": "用于验证等待状态以及关闭面板后不重新弹出。",
                },
                {
                    "title": "<img src=x onerror=alert(1)>（测试）",
                    "url": "javascript:alert(1)",
                    "snippet": "危险标题与地址应作为文本处理，不能执行。",
                },
            ],
            "meta": {"provider": "test-fixture", "cached": False},
        }
    if name == "read_page":
        if "failure" in args["url"]:
            return {"ok": False, "error": {"code": "page_not_found"}}
        return {
            "ok": True,
            "url": args["url"],
            "title": "Python 异步编程指南（测试）",
            "content": ARTICLE,
            "extracted": True,
        }
    if name == "get_weather":
        return {
            "ok": True,
            "resolved_location": "浙江省·杭州（测试）",
            "forecast_days": [
                {
                    "date": "2026-11-10",
                    "high_c": 23,
                    "low_c": 15,
                    "day_weather": "晴",
                    "day_wind_direction": "东北风",
                    "day_wind_power": "2 级",
                }
            ],
            "hourly": [{"time": "2026-11-10T10:00+08:00", "weather": "晴", "temp_c": 21, "pop": 10}],
            "fetched_at": "2026-11-09T20:00:00+08:00",
            "limitations": [],
        }
    if name == "place_search":
        return {
            "ok": True,
            "places": [
                {
                    "name": "西湖咖啡馆（测试）",
                    "address": "浙江省杭州市西湖区",
                    "rating": 4.5,
                    "reference_cost_yuan": 38,
                    "map_url": "https://example.com/place",
                }
            ],
            "providers": ["test-fixture"],
            "limitations": [],
        }
    if name == "route_compare":
        return {
            "ok": True,
            "origin": {"label": args["origin"]},
            "destination": {"label": args["destination"]},
            "routes": [{"mode": "driving", "duration_s": 3600, "distance_m": 28000, "toll_yuan": 0}],
            "unavailable_modes": ["transit"],
            "limitations": [],
        }
    if name in {"search_flights", "search_trains"}:
        return {
            "ok": True,
            "services": [
                {
                    "number": "G123（测试）",
                    "train_type": "高铁",
                    "departure": {"city": args["origin"], "station": args["origin"] + "站", "time": "2026-11-10T10:00"},
                    "arrival": {
                        "city": args["destination"],
                        "station": args["destination"] + "站",
                        "time": "2026-11-10T11:00",
                        "day_offset": 0,
                    },
                    "duration_minutes": 60,
                    "price_yuan": 99,
                    "travel_class": "二等座",
                }
            ],
            "limitations": ["filtered_partial_list"],
        }
    if name == "get_latest_news":
        return {
            "success": True,
            "data": [
                {
                    "title": "用于验证热榜展示的资讯（测试）",
                    "platform": "weibo",
                    "platform_name": "微博",
                    "rank": 1,
                    "timestamp": "2026-11-10 01:30:00",
                    "url": "https://example.com/news",
                }
            ],
        }
    return None


@app.get("/image/static/images/{filename}")
async def image(filename: str, request: Request):
    if request.headers.get("authorization") != "Bearer test-only":
        return Response(status_code=401)
    if filename != "11111111-1111-4111-8111-111111111111.png":
        return Response(status_code=404)
    return Response(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6XcAAAAASUVORK5CYII="
        ),
        media_type="image/png",
    )


@app.api_route("/{service}/{path:path}", methods=["POST", "DELETE"])
async def gateway(service: str, path: str, request: Request):
    if request.headers.get("authorization") != "Bearer test-only":
        return JSONResponse({"error": "unauthorized"}, 401)
    if request.method == "DELETE":
        return Response(status_code=204)
    payload = await request.json()
    if path == "v1/generate":
        return {
            "id": "fixture",
            "image_url": "/static/images/11111111-1111-4111-8111-111111111111.png",
            "model": "test-fixture",
            "cached": False,
        }
    method = payload["method"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-03-26"}}
    if method == "notifications/initialized":
        return Response(status_code=202)
    name, args = payload["params"]["name"], payload["params"]["arguments"]
    if name == "read_page" and "slow" in args["url"]:
        await asyncio.sleep(1)
    data = tool_result(name, args)
    result = (
        {"structuredContent": data}
        if data is not None
        else {
            "content": [
                {
                    "type": "text",
                    "text": "# FastAPI（测试）\n\nLibrary ID: `/fastapi/fastapi`\n\n使用 lifespan 管理应用生命周期。",
                }
            ]
        }
    )
    if name == "get_latest_news":
        # 与线上 FastMCP 的字符串返回契约一致，避免只测原生字典而漏掉包装层。
        text = json.dumps(data, ensure_ascii=False)
        result = {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text},
            "isError": False,
        }
    return {"jsonrpc": "2.0", "id": payload["id"], "result": result}
