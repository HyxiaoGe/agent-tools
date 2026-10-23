# trends

TrendRadar 的 MCP 服务（官方镜像 `wantcat/trendradar-mcp`），提供国内热榜和 RSS 的查询工具。经网关 `/trends/mcp` 访问。

- **数据**：来自 Sean 在 dev 上自行部署的 TrendRadar（`~/project/TrendRadar`，每 30 分钟抓一次）。这里只读挂载它的 `config/` 和 `output/`，不负责抓取。
- **镜像**：`Dockerfile` 只是 `FROM` 官方镜像。Windows 构建机拉取后推到 ACR，dev 从 ACR 拉取。升级时修改版本号，并与抓取端的版本配套。
- **工具**：镜像提供 27 个工具，其中 `send_notification`、`trigger_crawl`、`sync_from_remote` 等会产生副作用或属于运维用途。调用方只授权查询类工具（Fusion 在 MCP 设置里用 `allowed_tools` 白名单控制）。
- **冒烟**：`docker exec -i trends-mcp python - < scripts/smoke_mcp.py`
