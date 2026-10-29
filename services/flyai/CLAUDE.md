# flyai

FlyAI 航班 / 高铁查询的适配器，说明见 [README](README.md)。

- 第三方 CLI（`@fly-ai/flyai-cli`，版本与完整性哈希锁定）只在本容器内以子进程运行，每次调用独立临时 HOME、超时即杀进程组；`FLYAI_API_KEY` 只注入子进程环境，不进日志、不进响应。
- 只开放 `POST /v1/search/flights`、`/v1/search/trains` 与 `GET /health`，请求与响应都经严格 JSON 校验和字段投影，供应商原始字段不透出。
- 自身不鉴权，只接专用内网经网关访问；并发隔离按网关重写的 `X-Caller-App` / `X-Caller-User` 摘要：同一用户同时只跑一个 CLI，全局最多 2 个，超出返回 429 `concurrency_limited`。
- 改 `src/` 后跑 `npm test`；改依赖要同步 Dockerfile 里的版本与哈希校验。
