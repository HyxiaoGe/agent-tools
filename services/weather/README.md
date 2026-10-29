# weather

和风天气的只读代理。产品不持有和风凭证，经网关 `/weather/...` 调用这里，由这里用 Ed25519 私钥签 JWT 调和风 API，并把字段归一后返回。和风按项目计费，网关 `PRODUCTS` 里要给产品加上 `weather` 才放行（不在默认列表里）。

## 接口

| 接口 | 参数 | 返回 |
|---|---|---|
| `GET /v1/cities` | `location`（地名或“经度,纬度”，1–120 字）、`number`（1–20，默认 10）、`lang`（`zh` / `en`） | `{"cities": [{location_id, name, adm1, adm2, country, rank}]}`；和风查无结果（400/404）返回空列表 |
| `GET /v1/forecast/daily` | `location_id`（和风 LocationID 或“经度,纬度”） | `{"daily": [{date, text_day, text_night, temp_max, temp_min, wind_dir_day, wind_dir_night, wind_scale_day, wind_scale_night}]}`，7 天 |
| `GET /v1/forecast/hourly` | `location_id` | `{"hourly": [{time, temp, text, pop, precip}]}`，24 小时 |
| `GET /health` | | `{"status": "ok", "configured": true}`；`configured` 为 false 表示凭证不全 |

城市选择（模糊候选如何对应用户地名）、缓存与结果格式留在产品侧，这里只做鉴权与字段归一。

## 失败

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

部署冒烟：`docker exec -i weather-service python - < scripts/smoke_weather.py`（查一次“北京”和它的逐日预报，消耗两次和风请求）。
