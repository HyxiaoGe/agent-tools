import ipaddress
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value:
            raise ValueError("请输入内容")
        return value


class Search(Arguments):
    query: str = Field(min_length=1, max_length=500)
    count: int = Field(5, ge=1, le=10)
    recency_days: int | None = Field(None, ge=1, le=365)


class Read(Arguments):
    url: str = Field(min_length=1, max_length=4096)
    full_page: bool = False

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("请输入公开网页的 HTTP(S) 地址")
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("不支持本地或内网地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("不支持本地或内网地址")
        # DNS 与重定向检查由现有 reader 工具执行，这里不直接访问用户提供的网址。
        return value


class Generate(Arguments):
    prompt: str = Field(min_length=1, max_length=3000)
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "1:1"
    quality: Literal["standard", "high"] = "standard"


class Trends(Arguments):
    limit: int = Field(10, ge=1, le=30)
    include_url: Literal[True] = True


class Places(Arguments):
    query: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    anchor_source: Literal["none"] = "none"
    limit: int = Field(5, ge=1, le=5)


class Route(Arguments):
    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    origin_city: str = Field(min_length=1, max_length=80)
    destination_city: str | None = Field(None, min_length=1, max_length=80)
    origin_source: Literal["named"] = "named"
    destination_source: Literal["named"] = "named"
    modes: list[Literal["driving", "transit", "walking", "bicycling"]] = Field(
        default_factory=lambda: ["driving", "transit"], min_length=1, max_length=3
    )


class Weather(Arguments):
    location: str = Field(min_length=1, max_length=120)
    requested_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class Flights(Arguments):
    origin: str = Field(min_length=1, max_length=80)
    destination: str = Field(min_length=1, max_length=80)
    departure_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    limit: int = Field(5, ge=1, le=5)

    @field_validator("departure_date")
    @classmethod
    def future_date(cls, value):
        day = datetime.strptime(value, "%Y-%m-%d").date()
        if day < datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("出发日期不能早于北京时间今天")
        return value


class Trains(Flights):
    train_category: Literal["high_speed", "all"] = "high_speed"


class Resolve(Arguments):
    libraryName: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)


class Docs(Arguments):
    libraryId: str = Field(min_length=1, max_length=200, pattern=r"^/")
    query: str = Field(min_length=1, max_length=1000)


CATEGORIES = [
    {
        "id": "search",
        "title": "联网搜索",
        "en": "Web search",
        "icon": "search",
        "providers": ["Brave", "Tavily", "Firecrawl", "DeepSeek"],
        "description": "从一个问题出发，找到来源，再读到原文。",
        "tags": ["MCP", "REST"],
    },
    {
        "id": "reader",
        "title": "网页读取",
        "en": "Page reader",
        "icon": "file",
        "providers": ["直连", "Firecrawl", "Tavily", "Jina"],
        "description": "把网页整理成清晰的 Markdown，专注正文。",
        "tags": ["MCP", "REST"],
    },
    {
        "id": "image",
        "title": "图像生成",
        "en": "Image generation",
        "icon": "image",
        "providers": ["Seedream", "Gemini"],
        "description": "把文字变成画面，试试不同构图与创意。",
        "tags": ["MCP", "REST"],
    },
    {
        "id": "trends",
        "title": "热榜资讯",
        "en": "Trending now",
        "icon": "trending",
        "providers": ["TrendRadar"],
        "description": "浏览最近一次抓取的热榜，发现正在发生的事。",
        "tags": ["MCP"],
    },
    {
        "id": "maps",
        "title": "地图与地点",
        "en": "Places & routes",
        "icon": "pin",
        "providers": ["高德地图", "腾讯地图"],
        "description": "发现城市里的地点，比较不同的出行方式。",
        "tags": ["MCP"],
    },
    {
        "id": "weather",
        "title": "天气预报",
        "en": "Weather forecast",
        "icon": "sun",
        "providers": ["和风天气"],
        "description": "查一座城市的天气，也看看接下来的每个小时。",
        "tags": ["MCP", "REST"],
    },
    {
        "id": "travel",
        "title": "航班与铁路",
        "en": "Flights & trains",
        "icon": "plane",
        "providers": ["FlyAI"],
        "description": "查询单程直达班次，比较时刻与参考价格。",
        "tags": ["MCP", "REST"],
    },
    {
        "id": "docs",
        "title": "技术文档",
        "en": "Developer docs",
        "icon": "code",
        "providers": ["Context7"],
        "description": "找到你用的库，查阅相关文档与代码示例。",
        "tags": ["MCP"],
    },
]


def field(name, label, kind="text", **options):
    return {"name": name, "label": label, "type": kind, **options}


TOOLS = {
    "search_web": {
        "category": "search",
        "title": "搜索网页",
        "service": "search",
        "model": Search,
        "fields": [
            field("query", "你想了解什么？", "textarea", placeholder="例如：Python 异步编程最佳实践", required=True),
            field("count", "结果数量", "select", options=[3, 5, 10], default=5),
            field(
                "recency_days",
                "时间范围",
                "select",
                options=[
                    {"value": "", "label": "不限时间"},
                    {"value": 7, "label": "最近一周"},
                    {"value": 31, "label": "最近一个月"},
                    {"value": 365, "label": "最近一年"},
                ],
            ),
        ],
        "examples": [
            {"label": "探索 Python", "arguments": {"query": "Python asyncio 官方文档"}},
            {"label": "城市漫游", "arguments": {"query": "杭州 西湖 徒步路线"}},
        ],
    },
    "read_page": {
        "category": "reader",
        "title": "读取网页",
        "service": "reader",
        "model": Read,
        "fields": [
            field("url", "网页地址", "url", placeholder="https://…", required=True),
            field("full_page", "保留整页内容", "checkbox", help="表格或列表较多时可开启"),
        ],
        "examples": [
            {"label": "Python 官网", "arguments": {"url": "https://www.python.org/"}},
            {"label": "FastAPI 文档", "arguments": {"url": "https://fastapi.tiangolo.com/"}},
        ],
    },
    "generate_image": {
        "category": "image",
        "title": "生成图片",
        "service": "image",
        "path": "/image/v1/generate",
        "model": Generate,
        "fields": [
            field(
                "prompt",
                "描述你想看到的画面",
                "textarea",
                placeholder="海边岩石上的灯塔，落日余晖，电影感摄影",
                required=True,
            ),
            field("aspect_ratio", "画面比例", "select", options=["1:1", "16:9", "9:16", "4:3", "3:4"], default="1:1"),
            field(
                "quality",
                "生成质量",
                "select",
                options=[{"value": "standard", "label": "标准"}, {"value": "high", "label": "高质量"}],
                default="standard",
            ),
        ],
        "examples": [
            {
                "label": "海岸灯塔",
                "arguments": {
                    "prompt": "海边岩石上的白色灯塔，日落，暖色天空，写实摄影，宁静的氛围",
                    "aspect_ratio": "16:9",
                },
            },
            {
                "label": "纸上花园",
                "arguments": {"prompt": "一座微缩纸艺花园，层叠的绿色植物，柔和自然光，米白背景，精致手工质感"},
            },
        ],
    },
    "get_latest_news": {
        "category": "trends",
        "title": "查看热榜",
        "service": "trends",
        "model": Trends,
        "fields": [field("limit", "资讯数量", "select", options=[10, 20, 30], default=10)],
        "examples": [{"label": "看看最近热榜", "arguments": {"limit": 10}}],
    },
    "place_search": {
        "category": "maps",
        "title": "搜索地点",
        "service": "maps",
        "model": Places,
        "fields": [
            field("city", "城市", required=True, placeholder="杭州"),
            field("query", "寻找什么地点？", required=True, placeholder="西湖附近的咖啡馆"),
        ],
        "examples": [{"label": "杭州咖啡馆", "arguments": {"city": "杭州", "query": "西湖 咖啡馆"}}],
    },
    "route_compare": {
        "category": "maps",
        "title": "比较路线",
        "service": "maps",
        "model": Route,
        "fields": [
            field("origin_city", "出发城市", required=True, placeholder="上海"),
            field("origin", "起点", required=True, placeholder="虹桥火车站"),
            field("destination", "终点", required=True, placeholder="外滩"),
            field("destination_city", "到达城市（跨城时填写）", placeholder="默认同出发城市"),
            field(
                "modes",
                "出行方式",
                "multiselect",
                options=[
                    {"value": "driving", "label": "驾车"},
                    {"value": "transit", "label": "公交"},
                    {"value": "walking", "label": "步行"},
                    {"value": "bicycling", "label": "骑行"},
                ],
                default=["driving", "transit"],
            ),
        ],
        "examples": [
            {"label": "虹桥到外滩", "arguments": {"origin_city": "上海", "origin": "虹桥火车站", "destination": "外滩"}}
        ],
    },
    "get_weather": {
        "category": "weather",
        "title": "查询天气",
        "service": "weather",
        "model": Weather,
        "fields": [
            field("location", "城市或地区", required=True, placeholder="杭州"),
            field(
                "requested_date",
                "指定日期（可选）",
                "date",
                placeholder="自动查询近期天气",
                help="默认查询近期预报；选择日期后查询当天。",
            ),
        ],
        "examples": [
            {"label": "杭州天气", "arguments": {"location": "杭州"}},
            {"label": "上海天气", "arguments": {"location": "上海"}},
        ],
    },
    "search_flights": {
        "category": "travel",
        "title": "查航班",
        "service": "flyai",
        "model": Flights,
        "fields": [
            field("origin", "出发城市", required=True, placeholder="北京"),
            field("destination", "到达城市", required=True, placeholder="上海"),
            field("departure_date", "出发日期", "date", required=True),
        ],
        "examples": [{"label": "北京 → 上海", "arguments": {"origin": "北京", "destination": "上海"}}],
    },
    "search_trains": {
        "category": "travel",
        "title": "查火车",
        "service": "flyai",
        "model": Trains,
        "fields": [
            field("origin", "出发城市", required=True, placeholder="杭州"),
            field("destination", "到达城市", required=True, placeholder="上海"),
            field("departure_date", "出发日期", "date", required=True),
            field(
                "train_category",
                "车型",
                "select",
                options=[{"value": "high_speed", "label": "高铁 / 动车"}, {"value": "all", "label": "全部车型"}],
                default="high_speed",
            ),
        ],
        "examples": [{"label": "杭州 → 上海", "arguments": {"origin": "杭州", "destination": "上海"}}],
    },
    "resolve-library-id": {
        "category": "docs",
        "title": "查找库",
        "service": "context7",
        "model": Resolve,
        "fields": [
            field("libraryName", "库名称", required=True, placeholder="FastAPI"),
            field("query", "你想了解的问题", "textarea", required=True, placeholder="如何管理应用的生命周期？"),
        ],
        "examples": [
            {"label": "了解 FastAPI", "arguments": {"libraryName": "FastAPI", "query": "如何管理应用的生命周期？"}}
        ],
    },
    "query-docs": {
        "category": "docs",
        "title": "查询文档",
        "service": "context7",
        "model": Docs,
        "fields": [
            field("libraryId", "Context7 库 ID", required=True, placeholder="填写「查找库」返回的 ID"),
            field("query", "文档问题", "textarea", required=True, placeholder="如何使用 lifespan？"),
        ],
        "examples": [
            {
                "label": "FastAPI 生命周期",
                "arguments": {
                    "libraryId": "/websites/fastapi_tiangolo",
                    "query": "如何使用 lifespan 管理应用的生命周期？",
                },
            }
        ],
    },
}


def catalog(configured):
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).date().isoformat()
    tools = []
    for name, tool in TOOLS.items():
        fields = []
        defaults = {}
        for item in tool["fields"]:
            model_field = tool["model"].model_fields[item["name"]]
            value = "" if model_field.is_required() else model_field.get_default(call_default_factory=True)
            if item["name"] == "departure_date":
                value = tomorrow
            value = item.get("default", value)
            defaults[item["name"]] = "" if value is None else value
            fields.append({**item, "default": defaults[item["name"]]})
        tools.append(
            {
                "id": name,
                **{key: value for key, value in tool.items() if key not in {"model", "fields", "examples"}},
                "fields": fields,
                "defaults": defaults,
                # 示例始终是一整套表单参数，切换时不继承上一个示例的筛选。
                "examples": [
                    {**example, "arguments": {**defaults, **example["arguments"]}} for example in tool["examples"]
                ],
            }
        )
    return {
        "configured": configured,
        "categories": CATEGORIES,
        "tools": tools,
        "default_departure_date": tomorrow,
    }
