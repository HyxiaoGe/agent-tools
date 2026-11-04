# flyai

FlyAI 航班 / 高铁查询的适配器，说明见 [README](README.md)。

- 第三方 CLI（`@fly-ai/flyai-cli`，版本与完整性哈希锁定）只在本容器内以子进程运行，每次调用独立临时 HOME、超时即杀进程组；`FLYAI_API_KEY` 只注入子进程环境，不进日志、不进响应。
- 开放 `POST /mcp`（工具 `search_flights`、`search_trains`）、`POST /v1/search/flights`、`/v1/search/trains`（REST，Fusion 在用）、`GET /usage/daily` 与 `GET /health`；请求与响应都经校验和字段投影，供应商原始字段不透出。
- MCP 的参数、调用 CLI 的参数和结果规整在 `src/travel.js`，工具说明（英文，两个产品共用）和给模型的失败说明在 `src/mcp.js`；不点名别的工具、不提卡片，产品专有的话由产品接在后面。
- 没启动 CLI 的响应要带 `x-upstream-calls: 0`（网关据此不计入每日上限）；新加的提前返回路径记得覆盖到。
- 自身不鉴权，只接专用内网经网关访问；并发隔离按网关重写的 `X-Caller-App` / `X-Caller-User` 摘要：同一用户同时只跑一个 CLI，全局最多 2 个，REST 和 MCP 共用一个闸门（REST 满了直接 429，MCP 排队 6 秒）。
- 设备标识和每日计数存在状态目录（命名卷 `/var/lib/flyai`），别删卷；改 Dockerfile 时保留这个目录的属主 `node`。
- 改 `src/` 后跑 `npm test`；改依赖要同步 Dockerfile 里的版本与哈希校验。测试只用 `test/fixtures/` 的录制样本，不调真实服务；新录样本要先把预订链接换成假路径。
