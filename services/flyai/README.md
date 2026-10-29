# flyai

FlyAI 航班 / 高铁查询的适配器：把第三方 CLI 包成两个只读 HTTP 接口，服务端持有 API Key，调用方经网关 `/flyai/...` 访问。原为 [Fusion](https://github.com/HyxiaoGe/fusion) 内的 `flyai-adapter`，迁到这里后 Fusion 只带产品密钥。

| 接口 | 说明 |
|---|---|
| `POST /v1/search/flights` | 航班查询：`origin`、`destination`、`departure_date` 必填，可选 `cabin_class`、`max_price_yuan`、`departure_hour_start/end`、`sort_by`、`limit`（≤5） |
| `POST /v1/search/trains` | 高铁 / 火车查询：参数同上，`cabin_class` 换成 `seat_class` |
| `GET /health` | 健康检查，不触发供应商调用 |

成功返回 `{"observed_at", "request", "items":[...]}`，`items` 只含投影后的安全字段（车次 / 航班号、承运方、出发到达站点与时刻、时长、舱位 / 座席、价格、预订链接）。失败返回 `{"error_code": ...}`：

| 状态 | `error_code` | 含义 |
|---|---|---|
| 400 | `invalid_request` | 参数不合法，或缺少网关身份头 |
| 429 | `concurrency_limited` | 同一用户已有查询在跑，或全局并发已满（2） |
| 502 | `provider_error` / `invalid_provider_response` / `provider_output_too_large` | 供应商 CLI 失败或输出不合法 |
| 503 | `credential_unavailable` | 未配置 `FLYAI_API_KEY` |
| 504 | `provider_timeout` | 供应商 CLI 超过 20 秒 |

## 隔离

- CLI 以子进程运行，`detached` 进程组，每次调用独立临时 HOME，结束即删除；超时或请求取消先 SIGTERM 再 SIGKILL。
- 容器只读根文件系统、`/tmp` 为 noexec tmpfs、`cap_drop: ALL`、`pids_limit 64`、内存 256M。
- 自身不鉴权，只在 `agent-tools-internal` 网络里；并发隔离按网关重写的 `X-Caller-App` / `X-Caller-User` 的 sha256 摘要，用户标识不落盘不进日志。

## 开发

```bash
npm ci
npm test
```

配置见 `.env.example`。
