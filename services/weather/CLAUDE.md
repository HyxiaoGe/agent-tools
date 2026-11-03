# weather

和风天气服务，说明见 [README](README.md)。给模型用的 MCP 工具 `get_weather` 在 `app/forecast.py`（逻辑）和 `app/mcp_server.py`（说明、结果格式）；REST `/v1/...` 是线上 Fusion 在用的。

- 凭证只在这里签 JWT 发给和风；令牌、响应体不进日志（httpx 日志已压到 WARNING），不进报错。
- 只请求 `app/qweather.py` 里的三个固定路径，不跟随重定向、不重试；`location_id` 只接受数字 / 经纬度字符，不让调用方拼出别的路径或参数。
- 字段在这里归一（`City` / `Day` / `Hour`），调用方不接触和风原始字段；REST 的返回不改（线上 Fusion 在用），新字段只给 `get_weather` 内部用。
- 2026-11-04 起推翻了“城市选择、缓存、结果呈现留在产品侧”：这些挪进 `get_weather`，Fusion 和灵稿共用。改结果字段或说明会影响两个产品的模型和页面，要同步 README，并告诉两边。
- 给模型看的说明（英文）不点名别的工具、不提卡片、不写工具自己的名字；产品专有的话由产品接在后面。
- 失败一律 `retryable: false`，服务这边的问题只告诉模型“没有可用结果”，不让它把工具状态讲给用户。地名、日期不进日志。
- 凭证不全时照常启动并返回 503 `weather_not_configured`；私钥或主机无效要在启动时失败，不能静默降级。
