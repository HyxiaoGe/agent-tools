# trends

TrendRadar 的 MCP 服务（官方镜像 `wantcat/trendradar-mcp`），提供国内热榜和 RSS 的查询工具。经网关 `/trends/mcp` 访问。

- **数据**：来自 Sean 在 dev 上自行部署的 TrendRadar（`~/project/TrendRadar`，每 30 分钟抓一次）。这里只读挂载它的 `config/` 和 `output/`，不负责抓取。
- **镜像**：`Dockerfile` 基于官方镜像，只加了 `contract_server.py` 作为启动入口。Windows 构建机拉取后推到 ACR，dev 从 ACR 拉取。升级时修改版本号，并与抓取端的版本配套。
- **契约补齐**（`contract_server.py`，用 FastMCP 中间件，不改上游代码）：
  - 上游参数定义里，`mode`、`sort_by`、`search_mode` 等只写了 string，可选值只出现在说明文字里，模型经常猜错。列工具时把可选值写进 `enum`；`platforms` 的可选值按 `config.yaml` 当前配置实时给出。升级镜像时要按上游校验逻辑重新核对这些取值。
  - 上游出错时返回 `{"success": false, "error": {...}}`，但不标记 `isError`，调用方会当作成功。这里改为工具错误，错误码和建议原样带回。
- **工具**：镜像提供 27 个工具，其中 `send_notification`、`trigger_crawl`、`sync_from_remote` 等会产生副作用或属于运维用途。调用方只授权查询类工具（Fusion 在 MCP 设置里用 `allowed_tools` 白名单控制）。
- **冒烟**：`docker exec -i trends-mcp python - < scripts/smoke_mcp.py`
