"""页面内置的固定演示资料；不导入供应商，也不随表单参数伪造实时结果。"""

SEARCH_URL = "https://docs.python.org/3/library/asyncio.html"

ARTICLE = """# Python asyncio 入门

这是一份用于展示网页正文排版的固定示例，不是本次抓取的网页。

## 协程、任务与事件循环

`asyncio` 用于编写并发代码。等待网络或文件操作时，可以让其他任务继续执行。

| 概念 | 用途 |
| --- | --- |
| 协程 | 描述可以暂停和恢复的异步操作 |
| 任务 | 调度协程执行 |
| 事件循环 | 协调任务与输入输出 |

[阅读官方文档](https://docs.python.org/3/library/asyncio.html)
"""

RESULTS = {
    "search_web": {
        "ok": True,
        "results": [
            {"title": "asyncio — 异步 I/O", "url": SEARCH_URL, "snippet": "了解 Python 协程、任务与事件循环。"},
            {
                "title": "协程与任务",
                "url": "https://docs.python.org/3/library/asyncio-task.html",
                "snippet": "从 async / await 开始，了解任务调度与并发执行。",
            },
            {
                "title": "asyncio 运行器",
                "url": "https://docs.python.org/3/library/asyncio-runner.html",
                "snippet": "了解异步程序的运行入口与生命周期。",
            },
        ],
        "meta": {"provider": "示例资料"},
    },
    "read_page": {
        "ok": True,
        "title": "Python asyncio 入门 · 示例正文",
        "url": SEARCH_URL,
        "content": ARTICLE,
        "extracted": True,
    },
    "generate_image": {
        "image_url": "/assets/examples/lighthouse.jpeg",
        "model": "Seedream · 历史生成样例",
    },
    "get_latest_news": {
        "success": True,
        "data": [
            {"title": "开源工具如何帮助开发者整理信息（示例）", "platform": "科技资讯", "rank": 1},
            {"title": "周末城市漫游：发现身边的新去处（示例）", "platform": "生活方式", "rank": 2},
            {"title": "从一个想法到一张图：体验生成式创作（示例）", "platform": "创意观察", "rank": 3},
        ],
    },
    "place_search": {
        "ok": True,
        "providers": ["示例资料"],
        "places": [
            {
                "name": "湖畔咖啡馆（虚构示例）",
                "address": "杭州市西湖周边 · 示例地址",
                "rating": 4.6,
                "reference_cost_yuan": 38,
            },
            {
                "name": "绿荫书店咖啡（虚构示例）",
                "address": "杭州市西湖区 · 示例地址",
                "rating": 4.5,
                "reference_cost_yuan": 45,
            },
        ],
    },
    "route_compare": {
        "ok": True,
        "origin": {"label": "虹桥火车站"},
        "destination": {"label": "外滩"},
        "routes": [
            {
                "mode": "driving",
                "duration_s": 3600,
                "distance_m": 28000,
                "toll_yuan": 0,
                "summary": "沿城市道路前往目的地 · 示例路线",
            },
            {
                "mode": "transit",
                "duration_s": 4500,
                "distance_m": 26000,
                "transfers": 1,
                "summary": "地铁与步行组合 · 示例路线",
            },
        ],
    },
    "get_weather": {
        "ok": True,
        "resolved_location": "浙江省 · 杭州市",
        "forecast_days": [
            {
                "date": "示例第 1 天",
                "high_c": 23,
                "low_c": 15,
                "day_weather": "晴",
                "day_wind_direction": "东北风",
                "day_wind_power": "2 级",
            },
            {
                "date": "示例第 2 天",
                "high_c": 22,
                "low_c": 14,
                "day_weather": "多云",
                "day_wind_direction": "东风",
                "day_wind_power": "2 级",
            },
            {
                "date": "示例第 3 天",
                "high_c": 20,
                "low_c": 13,
                "day_weather": "小雨",
                "day_wind_direction": "东风",
                "day_wind_power": "3 级",
            },
        ],
        "hourly": [
            {"time": "2026-11-10T10:00+08:00", "weather": "晴", "temp_c": 20, "pop": 0},
            {"time": "2026-11-10T13:00+08:00", "weather": "晴", "temp_c": 23, "pop": 5},
            {"time": "2026-11-10T16:00+08:00", "weather": "多云", "temp_c": 21, "pop": 10},
        ],
    },
    "search_flights": {
        "ok": True,
        "services": [
            {
                "number": "航班示例 A",
                "airline": "示例航空",
                "travel_class": "经济舱",
                "departure": {"station": "北京首都", "time": "2026-11-11T09:00"},
                "arrival": {"station": "上海虹桥", "time": "2026-11-11T11:15"},
                "duration_minutes": 135,
                "price_yuan": 680,
            },
            {
                "number": "航班示例 B",
                "airline": "示例航空",
                "travel_class": "经济舱",
                "departure": {"station": "北京大兴", "time": "2026-11-11T14:30"},
                "arrival": {"station": "上海浦东", "time": "2026-11-11T16:50"},
                "duration_minutes": 140,
                "price_yuan": 720,
            },
        ],
    },
    "search_trains": {
        "ok": True,
        "services": [
            {
                "number": "高铁示例 A",
                "train_type": "高铁",
                "travel_class": "二等座",
                "departure": {"station": "杭州东", "time": "2026-11-11T09:00"},
                "arrival": {"station": "上海虹桥", "time": "2026-11-11T10:00"},
                "duration_minutes": 60,
                "price_yuan": 73,
            },
            {
                "number": "高铁示例 B",
                "train_type": "高铁",
                "travel_class": "二等座",
                "departure": {"station": "杭州东", "time": "2026-11-11T10:30"},
                "arrival": {"station": "上海虹桥", "time": "2026-11-11T11:35"},
                "duration_minutes": 65,
                "price_yuan": 73,
            },
        ],
    },
    "resolve-library-id": {
        "content": (
            "# FastAPI\n\n**库 ID：** `/websites/fastapi_tiangolo`\n\n"
            "用于构建 Python API 的 Web 框架。\n\n"
            "这是固定的库检索展示样例；真实查询会返回当时匹配的库与说明。"
        ),
    },
    "query-docs": {
        "content": (
            "# 管理应用的生命周期\n\n使用 `lifespan` 管理启动和关闭时的资源：\n\n"
            "1. 启动时建立连接或加载共享资源。\n2. 让应用处理请求。\n3. 关闭时释放资源。\n\n"
            "这是固定文档排版样例，不是本次查询返回的文档。\n\n"
            "[查看 FastAPI 官方文档](https://fastapi.tiangolo.com/advanced/events/)"
        ),
    },
}
