# agent-tools

给 Fusion 等产品的 Agent 调用的工具服务，一个仓库、各自独立部署。调用方统一走 `gateway`（一个地址、一把产品密钥），由它鉴权、按产品放行服务、记账并按路径转发给各服务：

| 目录 | 服务 | dev 端口 | 说明 |
|---|---|---|---|
| `gateway` | agent-tools-gateway | 8100（仅本机；middleware_default 内按容器名访问；外部经 `agent-tools.dev.seanfield.org`） | 统一入口：产品密钥鉴权、调用记账、`/image` `/search` `/reader` `/trends` 转发 |
| `services/image` | image-service | 8090（host 网络） | 生图 / 改图，REST + MCP，产品密钥鉴权与用量记账 |
| `services/search` | search-service | 8080（仅 agent-tools-internal） | 多搜索源统一检索；自带 `search-redis` 存缓存与 Firecrawl 每日用量 |
| `services/reader` | reader-service | 8091（仅 agent-tools-internal） | 网页正文读取 |
| `services/trends` | trends-mcp | 3333（仅 agent-tools-internal） | TrendRadar 热榜 MCP（官方镜像转存，只读 dev 上 TrendRadar 的数据） |

search / reader / trends 自身不鉴权，只接入专用网络 `agent-tools-internal`（流水线 sync 步骤创建），只有网关能访问；其他容器和监控都经网关。各服务健康检查 `/search/health`、`/reader/health`、`/image/v1/health` 经网关免密钥、不记账。

各服务的开发命令、配置与架构见各自目录下的 README / CLAUDE.md。

## 流水线

`.github/workflows/pipeline.yml`：PR 和 push `main` 时只在 dev 上检查有改动的服务；合入 `main` 后：

1. Windows 构建机（runner 标签 `agent-tools-build`）构建有改动的服务镜像，推到 ACR `seanfield/agent-tools-<服务>:<提交 SHA>`（`.github/scripts/windows-publish.ps1`）。
2. dev 上的部署目录 `~/project/agent-tools` 切到本次提交（compose 文件与 `.env`）。
3. dev 依次拉镜像部署（`.github/scripts/deploy-from-acr.sh`），并把镜像标成 `agent-tools/<服务>:current`。在 dev 上手动 `docker compose up -d` 时沿用这个版本，不会重新构建。

`.github/` 有改动时四个服务都跑。

手动重新部署（例如改了 dev 上的 `.env`）：

```bash
gh workflow run pipeline.yml -R HyxiaoGe/agent-tools -f service=<all|gateway|image|search|reader|trends>
```

dev 上每个服务的 `.env` 放在部署目录对应的 `services/<name>/.env`（网关是 `gateway/.env`），不入库；生图结果与用量库在 `services/image/storage`，网关用量库在 `gateway/storage`。

## 历史

三个服务分别从 `HyxiaoGe/image-service`、`search-service`、`reader-service` 用 `git subtree` 并入，保留原提交历史。
