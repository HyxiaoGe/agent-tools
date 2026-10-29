# context7

Context7 官方 MCP（`https://mcp.context7.com/mcp`）的代理。产品不持有 Context7 密钥，经网关 `/context7/mcp` 调用这里，由这里注入密钥（未配置时按匿名额度调用）、过滤工具、标记限流。密钥按账号限流，网关 `PRODUCTS` 里要给产品加上 `context7` 才放行（不在默认列表里）。

## 工具

列出并允许调用的文档工具（`app/server.py` 的 `DOCUMENTATION_TOOLS`）：

| 工具 | 用途 |
|---|---|
| `resolve-library-id` | 按库名解析 Context7 的库 ID |
| `query-docs` | 按库 ID 与问题查询最新文档片段 |

上游日后新增的其他工具不列出，按名字调用返回 `tool_not_allowed`。

## 限流

上游按密钥限流，被限流时这里保留上游原文，并在 `structuredContent.error` 里给出 `{"code": "rate_limited", "tool": "query-docs"}`；其他上游错误原样透传。Context7 响应慢（常见 5–20s），代理调用超时默认 45s，网关对 `/context7` 的超时更长，由调用方决定放弃。

## 运行

```bash
cp .env.example .env     # 填 CONTEXT7_API_KEY
uv sync --frozen && uv run python -m app.server
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

`GET /health` 不走 MCP 握手，经网关 `/context7/health` 免密钥。部署冒烟：`docker exec -i context7-mcp python - < scripts/smoke_mcp.py`（会消耗一次 `resolve-library-id` 请求）。
