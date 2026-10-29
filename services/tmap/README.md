# tmap

腾讯地图（腾讯位置服务）官方 MCP（`https://mcp.map.qq.com/mcp`）的代理。产品不持有腾讯密钥，经网关 `/tmap/mcp` 调用这里，由这里把密钥注入上游地址、过滤工具。腾讯按 key 计每日配额（地点搜索 200 次/天，输入提示、路线、距离矩阵等 6000 次/天，账号内共享），网关 `PRODUCTS` 里要给产品加上 `tmap` 才放行（不在默认列表里）。

## 工具

列出并允许调用的只读工具（`app/server.py` 的 `READ_ONLY_TOOLS`）：

| 工具 | 用途 |
|---|---|
| `placeSuggestion` | 关键词输入提示，返回地点名称、地址、经纬度 |
| `placeSearchNearby` | 周边搜索，带距离 |
| `placeDetail` | 按地点 ID 查详情 |
| `placeAlongby` | 沿途搜索 |
| `reverseGeocoder` | 经纬度转地址 |
| `directionDriving` / `directionWalking` / `directionBicycling` / `directionTransit` | 驾车、步行、骑行、公交路线 |
| `futureDrivingDirection` | 未来出发时间的驾车路线 |
| `waypointOrder` | 途经点顺序优化 |
| `matrix` | 距离矩阵 |

不放行：`geocoder`（任何地址都返回 0,0 并标注结果不可信）、`weather`（天气由 [weather](../weather/README.md) 服务提供）、`ipLocation`（定位到的是服务器出口 IP）。上游日后新增的工具同样不列出，按名字调用返回 `tool_not_allowed`。

## 注意

- 上游输出是语义化文本（format=0），这里原样透传，不改格式。
- 控制台没给 key「分配额度」时上游不报错，统一返回「未找到结果」/「无路线信息」（isError=false），而同一个 key 直连 `apis.map.qq.com` 正常。遇到全部空结果先查控制台额度分配。

## 运行

```bash
cp .env.example .env     # 填 TMAP_API_KEY
uv sync --frozen && uv run python -m app.server
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

`GET /health` 不走 MCP 握手，经网关 `/tmap/health` 免密钥。部署冒烟：`docker exec -i tmap-mcp python - < scripts/smoke_mcp.py`（消耗一次 `placeSuggestion` 配额）。
