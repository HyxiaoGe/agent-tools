# amap

高德官方 MCP（`https://mcp.amap.com/mcp`）的代理。产品不持有高德密钥，经网关 `/amap/mcp` 调用这里，由这里注入密钥、过滤工具、标记配额用尽。网关 `PRODUCTS` 里要给产品加上 `amap` 才放行（付费服务不在默认列表里）。

## 工具

列出并允许调用的只读工具（`app/server.py` 的 `READ_ONLY_TOOLS`）：

| 工具 | 用途 | 月配额组 |
|---|---|---|
| `maps_geo` / `maps_regeocode` | 地理编码、逆地理编码 | 各自 |
| `maps_text_search` / `maps_around_search` / `maps_search_detail` | 关键字搜索、周边搜索、POI 详情 | 共用 `search` |
| `maps_direction_driving` / `_walking` / `_bicycling` / `_transit_integrated` | 路线规划 | 各自 |
| `maps_distance` | 距离测量 | 各自 |
| `maps_coordinate_convert` | 本服务补的坐标转换（高德 Web 服务），设备 GPS 坐标转 GCJ-02 后再搜周边、算路线 | 各自 |

`maps_schema_*`（唤起高德 App）、`maps_weather`、`maps_ip_location` 等上游工具不列出，按名字调用返回 `tool_not_allowed`。

## 配额用尽

高德个人开发者按服务组计月配额，用尽时上游返回 `USER_DAILY_QUERY_OVER_LIMIT`（名字叫 DAILY，实际北京时间每月 1 日零点重置）。这里保留上游原文，并在 `structuredContent.error` 里给出：

```json
{"code": "quota_exhausted", "upstream_code": "USER_DAILY_QUERY_OVER_LIMIT", "quota_group": "search", "reset_at": "2026-11-01T00:00:00+08:00"}
```

调用方据此在 `reset_at` 前不再公告依赖该额度组的工具。其他上游错误原样透传。

## 运行

```bash
cp .env.example .env     # 填 AMAP_API_KEY
uv sync --frozen && uv run python -m app.server
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

`GET /health` 不走 MCP 握手，经网关 `/amap/health` 免密钥。部署冒烟：`docker exec -i amap-mcp python - < scripts/smoke_mcp.py`（会消耗一次 `maps_geo` 配额）。
