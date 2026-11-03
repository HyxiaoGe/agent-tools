# weather

和风天气服务。产品不持有和风凭证，经网关 `/weather/...` 调用这里，由这里用 Ed25519 私钥签 JWT 调和风 API。和风按项目计费，网关 `PRODUCTS` 里要给产品加上 `weather` 才放行（不在默认列表里）。

给模型用的是 MCP 工具 `get_weather`（`/weather/mcp`）：挑城市、缓存、整理结果、给模型的说明都在这里，产品直接把它交给模型。原来的 REST 接口（`/v1/...`）保留，线上 Fusion 在用，返回不变。

## MCP 工具 get_weather

2026-11-04 起，Fusion 和灵稿原先各写一份的天气逻辑挪到这里共用（`app/forecast.py`）。以 Fusion 那份为底，带上灵稿后来的改进。

参数（模型填）：

- `location`：地名，1–120 字。不收坐标和 6 位行政区代码；当前位置不在模型参数里，坐标该由产品从可信来源给（Fusion 迁过来时再加）。
- `requested_date`：可选，`YYYY-MM-DD`。用户问确定的一天时传，相对说法由模型按今天换算；超出预报范围也照传。

服务里做的：

- 挑城市：和风城市查询是模糊匹配，只认名字出现在地名里的候选；同名的用地名里写到的省、市、国家区分；还分不出就返回候选，不按排名猜。照候选的写法（`江西省·南昌·西湖`）再查时只拿最后一段去查；纯英文地名用英文查。
- 当地今天按城市时区（和风城市查询给的 `tz`）算；逐日从当地今天起取 4 天。
- 问的那天在预报范围内只给那一天，逐小时也只留那天的小时；不在范围内保留整段，`limitations` 标 `requested_date_not_covered`。
- 逐小时只在没指定日期、问今天或明天时取（和逐日同时取），取不到标 `hourly_unavailable`，不算失败。
- 缓存在进程内，两个产品共用：预报按城市半小时，城市查询一天。整个调用 20 秒超时。

结果：`structuredContent` 是整份（给页面），文字是去掉逐小时明细的 JSON（给模型）。

```json
{"ok": true, "resolved_location": "浙江省·杭州", "local_today": "2026-10-08", "requested_date": "2026-10-09",
 "day_count": 1,
 "forecast_days": [{"date": "2026-10-09", "weekday": "Friday", "day_weather": "晴", "night_weather": "多云",
                    "high_c": 26, "low_c": 17, "day_wind_direction": "东风", "day_wind_power": "1-3级",
                    "night_wind_direction": "北风", "night_wind_power": "1-3级"}],
 "hourly": [{"time": "2026-10-09T00:00:00+08:00", "temp_c": 18, "weather": "多云", "pop": 10, "precip_mm": 0}],
 "hourly_summary": {"window": "... ~ ...", "temp_min_c": 16, "temp_max_c": 18, "max_pop_percent": 70,
                    "max_pop_time": "...", "precipitation_hours": 3, "first_precipitation_time": "...",
                    "last_precipitation_time": "...", "total_precip_mm": 1.8},
 "fetched_at": "2026-10-08T16:00:00+08:00", "limitations": []}
```

`limitations`：`requested_date_not_covered`、`fewer_days`（不到 4 天）、`hourly_unavailable`。`fetched_at` 是北京时间（模型把 UTC 时刻当本地时间念错过）。

失败：`structuredContent` 是 `{"ok": false, "error": {"code", "message", "retryable": false, "candidates"?}}`，`isError` 为 false（和 image 一样）。一律不可重试：同样的参数再查也不会好，该换别的来源。给模型的文字只有几种：

| `code` | 给模型的说明 |
|---|---|
| `ambiguous_location` | 带候选；上下文清楚就用候选原文再查，否则问用户 |
| `location_not_found` | 只查得到城市和区县；能从对话判断所属城市就再查，否则问用户 |
| `invalid_arguments` | 参数哪里不对 |
| `not_configured`、`upstream_*`、`invalid_response`、`call_timeout` | 没有可用结果，按已有信息回答、不编造（不让模型把工具状态讲给用户） |

工具说明（英文）写在 `app/mcp_server.py`，不点名别的工具、不提卡片、不写工具自己的名字；结果怎么显示、查不到时用哪个工具，由产品接在说明后面。

## REST 接口（线上 Fusion 在用，保持不变）

| 接口 | 参数 | 返回 |
|---|---|---|
| `GET /v1/cities` | `location`（地名或“经度,纬度”，1–120 字）、`number`（1–20，默认 10）、`lang`（`zh` / `en`） | `{"cities": [{location_id, name, adm1, adm2, country, rank}]}`；和风查无结果（400/404）返回空列表 |
| `GET /v1/forecast/daily` | `location_id`（和风 LocationID 或“经度,纬度”） | `{"daily": [{date, text_day, text_night, temp_max, temp_min, wind_dir_day, wind_dir_night, wind_scale_day, wind_scale_night}]}`，7 天 |
| `GET /v1/forecast/hourly` | `location_id` | `{"hourly": [{time, temp, text, pop, precip}]}`，24 小时 |
| `GET /health` | | `{"status": "ok", "configured": true}`；`configured` 为 false 表示凭证不全 |

## REST 失败

| 状态 | `error.code` | 含义 |
|---|---|---|
| 503 | `weather_not_configured` | 五项凭证不全，服务未启用 |
| 502 | `upstream_auth_failed` | 和风返回 401/403：凭证失效或项目无权限 |
| 429 | `upstream_rate_limited` | 和风限流 |
| 504 | `upstream_timeout` | 和风超过 `QWEATHER_TIMEOUT_SECONDS`（默认 8s） |
| 502 | `upstream_unavailable` / `invalid_response` | 和风其他错误、响应过大或格式不对 |
| 422 | | 参数不合法（FastAPI 校验） |

响应体与令牌都不进日志。

## 运行

```bash
cp .env.example .env     # 填五项和风凭证
uv sync --frozen && uv run python -m app.main
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

部署冒烟：`docker exec -i weather-service python - < scripts/smoke_weather.py`（REST 查一次“北京”和逐日预报；MCP 列工具、`get_weather` 查一次“北京”再带明天的日期查一次，第二次走缓存。共消耗五次和风请求）。
