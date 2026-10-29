# weather

和风天气的只读代理，说明见 [README](README.md)。

- 凭证只在这里签 JWT 发给和风；令牌、响应体不进日志（httpx 日志已压到 WARNING），不进报错。
- 只请求 `app/qweather.py` 里的三个固定路径，不跟随重定向、不重试；`location_id` 只接受数字 / 经纬度字符，不让调用方拼出别的路径或参数。
- 字段在这里归一（`City` / `Day` / `Hour`），调用方不接触和风原始字段；改字段要同步 README 与 Fusion 的 `app/services/weather`。
- 城市选择、缓存、结果呈现留在产品侧，这里不做业务判断。
- 凭证不全时照常启动并返回 503 `weather_not_configured`；私钥或主机无效要在启动时失败，不能静默降级。
