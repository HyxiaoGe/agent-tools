# trends

TrendRadar 的 MCP 服务（官方镜像 `wantcat/trendradar-mcp`），提供国内热榜和 RSS 的查询工具。经网关 `/trends/mcp` 访问。

- **数据**：来自 Sean 在 dev 上自行部署的 TrendRadar（`~/project/TrendRadar`，每 30 分钟抓一次）。这里只读挂载它的 `config/` 和 `output/`，不负责抓取。
- **镜像**：`Dockerfile` 基于官方镜像，只加了 `contract_server.py` 作为启动入口。Windows 构建机拉取后推到 ACR，dev 从 ACR 拉取。升级时修改版本号，并与抓取端的版本配套。
- **契约补齐**（`contract_server.py`，用 FastMCP 中间件，不改上游代码）：
  - 上游参数定义里，`mode`、`sort_by`、`search_mode` 等只写了 string，可选值只出现在说明文字里，模型经常猜错。列工具时把可选值写进 `enum`；`platforms` 的可选值按 `config.yaml` 当前配置实时给出。升级镜像时要按上游校验逻辑重新核对这些取值。
  - 上游出错时返回 `{"success": false, "error": {...}}`，但不标记 `isError`，调用方会当作成功。这里改为工具错误，错误码和建议原样带回。
  - 上游 `get_latest_news` 说是“最新一批”，实际读的是当天所有上过榜的标题，名次取第一次上榜时的，每条都标最新一次抓取的时间。越到晚上差得越多：按 11-04 的库取前 60 条，05:00 有 7 条已不在榜上，22:30 有 47 条。这里把取数据的那一步换成 `latest_news.py`：每个平台最近一次抓取在榜的条目、当时的名次；某个平台这次没抓成就用它上一次的；今天还没抓到时用昨天最后一批。工具名、参数、返回格式不变（Fusion 每日灵感在用）。
- **工具**：镜像提供 27 个工具，`contract_server.py` 启动时只留下 16 个查询类工具（`QUERY_TOOLS`，都只读本地数据），其余 11 个删掉：发通知、查通知渠道、立刻抓取、从远程同步、经 r.jina.ai 读任意网页、看配置和系统状态、查版本。网关只按服务放行、不分工具，所以不能交给调用方自己挡；删掉的工具不出现在列表里，点名调用回 `Unknown tool`。上游升级加的新工具默认不开放。上游启动横幅里写死的工具清单不代表实际开放的。
- **测试**：`uv run pytest`，只测自己写的 `latest_news.py`（按 TrendRadar 4.1.1 的建表语句造库）。依赖 TrendRadar 的库结构，升级镜像时先确认 `news_items`、`rank_history`、`platforms` 没变。
- **冒烟**：`docker exec -i trends-mcp python - < scripts/smoke_mcp.py`，其中会检查 `get_latest_news` 取 60 条时同一平台没有重复的名次。
