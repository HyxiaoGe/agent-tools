# 部署与运维

## 服务与端口

| 目录 | 服务 | dev 端口 | 说明 |
|---|---|---|---|
| `gateway` | agent-tools-gateway | 8100（仅本机；middleware_default 内按容器名访问；外部经 `agent-tools.dev.seanfield.org`） | 统一入口：产品密钥鉴权、调用记账、`/image` `/search` `/reader` `/trends` `/amap` `/context7` `/weather` `/flyai` `/tmap` 转发 |
| `services/image` | image-service | 8090（host 网络） | 生图 / 改图，REST + MCP，产品密钥鉴权与用量记账 |
| `services/search` | search-service | 8080（仅 agent-tools-internal） | 多搜索源统一检索；自带 `search-redis` 存缓存与 Firecrawl 每日用量 |
| `services/reader` | reader-service | 8091（仅 agent-tools-internal） | 网页正文读取 |
| `services/trends` | trends-mcp | 3333（仅 agent-tools-internal） | TrendRadar 热榜 MCP（官方镜像转存，只读 dev 上 TrendRadar 的数据） |
| `services/amap` | amap-mcp | 3334（仅 agent-tools-internal） | 高德官方 MCP 的代理：注入密钥、只读白名单、配额用尽标记；付费服务，产品要在网关 `PRODUCTS` 的 `services` 里写明 |
| `services/context7` | context7-mcp | 3335（仅 agent-tools-internal） | Context7 官方 MCP 的代理：注入密钥、文档工具白名单、限流标记；密钥按账号限流，产品要在网关 `PRODUCTS` 的 `services` 里写明 |
| `services/weather` | weather-service | 8092（仅 agent-tools-internal） | 和风天气的只读代理：持有 Ed25519 凭证签 JWT、城市查询与逐日 / 逐小时预报、字段归一；按项目计费，产品要在网关 `PRODUCTS` 的 `services` 里写明 |
| `services/flyai` | flyai-adapter | 8093（仅 agent-tools-internal） | FlyAI 航班 / 高铁查询适配器：第三方 CLI 以隔离子进程运行、持有 API Key、字段投影；按用户并发隔离；付费服务，产品要在网关 `PRODUCTS` 的 `services` 里写明 |
| `services/tmap` | tmap-mcp | 3336（仅 agent-tools-internal） | 腾讯地图（腾讯位置服务）官方 MCP 的代理：注入密钥、只读白名单；按 key 计每日配额，产品要在网关 `PRODUCTS` 的 `services` 里写明 |

search / reader / trends / amap / context7 / weather / flyai / tmap 自身不鉴权，只接入专用网络 `agent-tools-internal`（流水线 sync 步骤创建），只有网关能访问；其他容器和监控都经网关。各服务健康检查 `/search/health`、`/reader/health`、`/image/v1/health`、`/amap/health`、`/context7/health`、`/weather/health`、`/flyai/health`、`/tmap/health` 经网关免密钥、不记账。

## 流水线

`.github/workflows/pipeline.yml`：PR 和 push `main` 时只在 dev 上检查有改动的服务；合入 `main` 后：

1. Windows 构建机（runner 标签 `agent-tools-build`）用 `docker buildx bake` 并行构建有改动的服务镜像并推到 ACR `seanfield/agent-tools-<服务>:<提交 SHA>`（`.github/scripts/windows-publish.ps1`；构建容器直连外网，实测比走代理或国内镜像源快）。
2. dev 上的部署目录 `~/project/agent-tools` 切到本次提交（compose 文件与 `.env`）。
3. dev 并行拉镜像部署各服务（`.github/scripts/deploy-from-acr.sh`），网关最后部署。容器用本地短名 `agent-tools/<服务>:<短 SHA>` 运行（`docker ps` 里不显示 ACR 长地址），同时标成 `agent-tools/<服务>:current`，在 dev 上手动 `docker compose up -d` 时沿用这个版本，不会重新构建。

`.github/` 有改动时所有服务都跑检查，但只有自身目录有改动的服务才重新构建、推送和部署；需要全量重部用下面的手动触发 `service=all`。dev 上为 agent-tools 注册了两个 runner 实例（`github-runner-agent-tools` 与 `github-runner-agent-tools-2`），检查和部署作业两两并行。

手动重新部署（例如改了 dev 上的 `.env`）：

```bash
gh workflow run pipeline.yml -R HyxiaoGe/agent-tools -f service=<all|gateway|image|search|reader|trends|amap|context7|weather|flyai|tmap>
```

dev 上每个服务的 `.env` 放在部署目录对应的 `services/<name>/.env`（网关是 `gateway/.env`），不入库；生图结果与用量库在 `services/image/storage`，网关用量库在 `gateway/storage`。

## 历史

image / search / reader 三个服务分别从 `HyxiaoGe/image-service`、`search-service`、`reader-service` 用 `git subtree` 并入，保留原提交历史。
