# Search Service

[English](README.md)

轻量级、无状态的搜索中间件服务，封装第三方搜索 API，通过 REST API 和 MCP Server 提供统一的搜索接口。

## 特性

- **Provider 无关**：通过配置切换搜索后端（Brave、Tavily、Firecrawl、DeepSeek、SearXNG），无需改代码
- **多类型搜索**：Web、News、Image
- **MCP Server**：Claude Code 和 AI Agent 可直接调用搜索工具
- **Redis 缓存**：基于 TTL 的查询去重缓存
- **限流保护**：保护上游 API 配额
- **Docker Compose**：一键部署

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，填入 BRAVE_API_KEY、TAVILY_API_KEY 或 FIRECRAWL_API_KEY
docker compose up -d
```

服务启动后访问 `http://localhost:8080`。

## API

### POST /search

```bash
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world", "type": "web", "count": 5}'
```

响应：

```json
{
  "query": "hello world",
  "type": "web",
  "provider": "brave",
  "cached": false,
  "results": [
    {
      "title": "...",
      "url": "...",
      "description": "...",
      "published_at": "2026-01-01T00:00:00"
    }
  ]
}
```

### 其他接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/providers` | 列出可用的搜索 Provider |
| GET | `/usage/budget` | 各限额 provider 今日（北京时间）已用 / 上限 / 被拒次数 |
| GET | `/usage/firecrawl` | Firecrawl credits 使用情况；未配置 `FIRECRAWL_API_KEY` 时返回 `available=false` 且 credits 字段为 null |
| GET | `/usage/firecrawl/historical?by_api_key=true` | Firecrawl 历史 credits 使用情况；按 key 分组时会脱敏 API key |
| DELETE | `/cache` | 清空缓存 |

## MCP Server

服务在 `/mcp` 暴露 MCP 工具，供 AI Agent 使用：

- `search` — 通用搜索（web/news/image）
- `search_news` — 新闻搜索快捷方式
- `search_images` — 图片搜索快捷方式

Claude Code 集成配置：

```json
{
  "mcpServers": {
    "search": {
      "type": "http",
      "url": "http://<SERVER_IP>:8080/mcp"
    }
  }
}
```

## 配置项

完整配置见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEARCH_PROVIDER` | `brave` | 当前搜索 Provider |
| `SEARCH_FALLBACK_PROVIDERS` | 空 | 逗号分隔的备用链，如 `firecrawl,tavily`。主 provider 报错或结果少于 `count/2` 条时按顺序尝试，够数即停；自动跳过主 provider 本身和未配置 key 的，写了未知名字服务起不来。留空 = 注册顺序里第一个非主 provider |
| `BRAVE_API_KEY` | — | Brave Search API 密钥 |
| `TAVILY_API_KEY` | — | Tavily Search API 密钥 |
| `FIRECRAWL_API_KEY` | — | Firecrawl API 密钥 |
| `FIRECRAWL_API_URL` | `https://api.firecrawl.dev` | Firecrawl API 基础地址 |
| `DEEPSEEK_API_KEY` | — | DeepSeek 搜索 API 密钥 |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com` | DeepSeek API 基础地址 |
| `REDIS_URL` | `redis://search-redis:6379` | Redis 连接地址（docker compose 固定为自带的 search-redis） |
| `CACHE_TTL_WEB` | `600` | Web 搜索缓存时间（秒） |
| `CACHE_TTL_NEWS` | `300` | 新闻搜索缓存时间（秒） |
| `RATE_LIMIT_GLOBAL` | `40/second` | 全局限流 |
| `RATE_LIMIT_PER_IP` | `10/second` | 单 IP 限流 |
| `PROVIDER_DAILY_LIMITS` | `brave=100,tavily=100,firecrawl=500,deepseek=1000` | 每个 provider 每日上游调用上限（北京时间自然日）。主调用、fallback、MCP 每次真正请求上游都占一个名额，缓存命中不占。超额后跳过该 provider，所有候选都没额度时 `/search` 返回 503。未列出的 provider 不限，`0` 表示停用 |
| `BUDGET_ALERT_RATIO` | `0.8` | 用量达到上限的这个比例时预警一次（每个 provider 每天一次），达到上限时再告警一次 |
| `BUDGET_ALERT_WEBHOOK` / `BUDGET_ALERT_SIGN_SECRET` | — | 额度告警用的飞书自定义机器人 webhook 与签名密钥；留空只写日志 |

## 技术栈

Python 3.12 / FastAPI / FastMCP / Redis / structlog / Docker Compose
