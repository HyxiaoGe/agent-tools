# agent-tools

给 Fusion 等产品的 Agent 调用的工具服务，一个仓库、各自独立部署：

| 目录 | 服务 | dev 端口 | 说明 |
|---|---|---|---|
| `services/image` | image-service | 8090（host 网络） | 生图 / 改图，REST + MCP，产品密钥鉴权与用量记账 |
| `services/search` | search-service | 8080（middleware_default） | 多搜索源统一检索 |
| `services/reader` | reader-service | 8091 | 网页正文读取 |

各服务的开发命令、配置与架构见各自目录下的 README / CLAUDE.md。

## 流水线

`.github/workflows/pipeline.yml`：PR 和 push `main` 时只检查有改动的服务；合入 `main` 后把 dev 上的部署目录 `~/project/agent-tools` 切到本次提交，再依次部署有改动的服务。`.github/` 有改动时三个服务都跑。

手动重新部署（例如改了 dev 上的 `.env`）：

```bash
gh workflow run pipeline.yml -R HyxiaoGe/agent-tools -f service=<all|image|search|reader>
```

dev 上每个服务的 `.env` 放在部署目录对应的 `services/<name>/.env`，不入库；生图结果与用量库在 `services/image/storage`。

## 历史

三个服务分别从 `HyxiaoGe/image-service`、`search-service`、`reader-service` 用 `git subtree` 并入，保留原提交历史。
