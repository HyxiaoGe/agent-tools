# gateway

agent-tools 的统一入口。调用方只配一个地址和一把产品密钥，网关负责：

1. **鉴权**：`Authorization: Bearer <产品服务密钥>` 认定产品（`PRODUCTS` 配置，密钥只放环境变量）。认不出产品一律 401。
2. **按产品放行服务**：`PRODUCTS` 里每个产品可写 `services`（允许调用的服务名列表）；省略时为基础服务 `image` / `search` / `reader` / `trends`。之后接入的付费服务不在默认列表里，要逐个产品写明。调用未放行的服务返回 403 `service_not_allowed`，不转发、不记账，只留一条日志。
3. **记账**：每个请求记一行（产品、用户、场景、请求 ID、服务、路径、MCP 工具名、状态、耗时），存 `storage/usage.sqlite3`，保留 90 天。MCP 调用都打在同一个 `/mcp` 路径，网关从 JSON-RPC 请求体里取 `tools/call` 的工具名记下来，只用于按工具统计用量，不改转发；`tools/list`、`initialize` 等消息和 REST 请求的工具名为空。
4. **转发**：按路径前缀转到对应服务，路径其余部分原样保留。

| 网关路径 | 上游 |
|---|---|
| `/image/...` | image-service（如 `/image/mcp`、`/image/v2/mcp`、`/image/v1/generate`、`/image/v1/images/{id}`） |
| `/search/...` | search-service（如 `/search/search`、`/search/mcp`） |
| `/reader/...` | reader-service（如 `/reader/read`） |
| `/trends/...` | trends-mcp（TrendRadar 热榜 MCP，`/trends/mcp`） |
| `/amap/...` | amap-mcp（高德官方 MCP 的代理，`/amap/mcp`；付费服务，产品要在 `services` 里写明） |
| `/context7/...` | context7-mcp（Context7 官方 MCP 的代理，`/context7/mcp`；密钥按账号限流，产品要在 `services` 里写明） |
| `/weather/...` | weather-service（和风天气的只读代理，`/weather/v1/cities`、`/weather/v1/forecast/daily`、`/weather/v1/forecast/hourly`；按项目计费，产品要在 `services` 里写明） |
| `/flyai/...` | flyai-adapter（FlyAI 航班 / 高铁查询适配器，`POST /flyai/v1/search/flights`、`/flyai/v1/search/trains`；付费服务，产品要在 `services` 里写明） |
| `/tmap/...` | tmap-mcp（腾讯地图官方 MCP 的代理，`/tmap/mcp`；按 key 计每日配额，产品要在 `services` 里写明） |
| `/maps/...` | maps-service（地图组合层，`/maps/mcp`：地点搜索、路线对比两个工具，先高德后腾讯；付费服务，产品要在 `services` 里写明） |

`GET /health` 不需要密钥。生图结果图片 `GET|HEAD /image/static/...` 本身就是公开地址，也不需要密钥、不记账。路径里含 `.` / `..` 段一律 400，防止借公开前缀绕过鉴权。

## 调用方声明的请求头

| 请求头 | 格式 | 用途 |
|---|---|---|
| `X-Caller-User` | `[a-zA-Z0-9_.:@-]{1,128}` | 产品内的用户 ID，只用于统计 |
| `X-Caller-Scene` | `[a-z][a-z0-9_]{0,63}` | 场景，如 `chat_image`、`image_edit`、`chat_search` |
| `X-Request-ID` | `[a-zA-Z0-9_.:-]{1,128}` | 与调用方的 run 关联 |

格式不对返回 400。产品身份只来自密钥：调用方自带的 `X-Caller-App`、`X-Image-User` 等身份头会被丢弃，由网关重写后再转给上游——上游收到 `X-Caller-App` / `X-Caller-User` / `X-Caller-Scene`；image-service 额外收到原 `Authorization` 和 `X-Image-User` / `X-Image-Scene`（它仍按同一把产品密钥选方舟凭证、记生图用量）。

## 失败

上游返回的任何状态码原样透传。网关自己判定的失败：

| 状态 | `error` | 含义 |
|---|---|---|
| 503 | `upstream_busy` | 该服务连接池已满且排队超过 `POOL_WAIT_SECONDS` |
| 504 | `upstream_timeout` | 上游超过该服务的超时 |
| 502 | `upstream_unreachable` | 连不上上游 |
| 429 | `daily_limit_exceeded` | 该服务当天的调用已达每日上限，`Retry-After` 为距北京时间 0 点的秒数 |

每个服务有独立的连接池和超时，某个服务堆积慢请求不会拖住其他服务。

## 每日上限

最外层的兜底：防止批量实验或失控循环把付费上游打穿。`SERVICE_DAILY_LIMITS`（`服务=次数`，逗号分隔，北京时间自然日）控制各服务每天最多转发多少次调用，超出直接返回 429，不转发，记账为 `outcome=limited`。

- 默认 `search=2000,reader=1000,image=200,weather=1000,amap=1000,tmap=1000,maps=300,flyai=100`，约为 10-23 ~ 11-01 单日峰值的 5~25 倍（maps 是 11-04 新加的，暂定 300）；未列出的服务（免费的 trends、context7）不限，`0` 表示当天一律拒绝。写了不存在的服务名，网关起不来。
- 只算真正的调用：REST 请求和 MCP `tools/call`。MCP 握手（`initialize`、`tools/list`、SSE）不算也不拦，会话可以照常建立。产品按 id 取、删 image-service 存着的图（`GET` / `DELETE /image/v1/images/{id}`）也不算不拦：页面每看一次图就取一次，不是生图；日报里也不计入。
- 所有产品共用一个计数。计数在内存里，每天第一次用到时从用量记录补回当天已有的调用数，重启不清零；被拦下的请求不计入。
- 用到 `LIMIT_ALERT_RATIO`（默认 0.8）和用满时各推一次飞书巡检群（`LIMIT_ALERT_WEBHOOK` / `LIMIT_ALERT_SIGN_SECRET`，留空只写日志），每个服务每天各一次。
- 服务内部还有按上游的额度（search、image、reader 各自的 `PROVIDER_DAILY_LIMITS`），通常先触发。

## 用量

按产品、服务、场景、MCP 工具分组：

```bash
docker exec agent-tools-gateway python -m app.usage_report --app-id fusion --days 7
```

### 每日日报

配置了 `DAILY_REPORT_WEBHOOK` 时，北京时间每天 `DAILY_REPORT_HOUR`（默认 9）点后把前一天的汇总推到飞书「AI 成本&额度」群：

- 总调用 / 失败次数（附前一天总量），按服务（含 MCP 工具前 3）和按产品的调用量。用量过半或有拦截的服务会附上每日上限和被拦下的次数。MCP 的握手消息（initialize、tools/list、SSE）不算调用，只统计 `tools/call` 和 REST 请求。
- search 实际调了哪家上游、被额度拦下次数、转备用次数和 Firecrawl 剩余 credits：网关看不到这些，向 search 的 `/usage/daily` 拉取。
- image、reader 各上游的调用 / 每日上限 / 被拦次数：向 image 的 `/v1/quota`、reader 的 `/usage/daily` 拉取。只列有调用或被拦的上游；某个服务当天没被调用也没有上游计数时不出这一行，拉取失败时写「获取失败」，不影响其余部分。

哪天已发送记在 `DAILY_REPORT_STATE_PATH`（默认 `storage/daily_report_state.json`），重启不重发；发送失败每天最多尝试 3 次。手动执行：

```bash
docker exec agent-tools-gateway python -m app.daily_report --print            # 只打印昨天的日报
docker exec agent-tools-gateway python -m app.daily_report --day 2026-11-01 --test   # 加【测试】前缀实发，不影响定时发送
```

## 开发

```bash
uv run pytest
ruff check . && ruff format --check .
```

配置见 `.env.example`。
