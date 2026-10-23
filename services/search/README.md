# Search Service

[中文文档](README_CN.md)

A lightweight, stateless search middleware that wraps third-party search APIs and exposes a unified interface via REST API and MCP Server.

## Features

- **Provider-agnostic**: Swap search backends (Brave, Tavily, Firecrawl, SearXNG) via config, no code changes
- **Multi-type search**: Web, News, Image
- **MCP Server**: Direct tool access for Claude Code and AI agents
- **Redis caching**: Deduplicates queries with TTL-based cache
- **Rate limiting**: Protects upstream API quota
- **Docker Compose**: One-command deployment

## Quick Start

```bash
cp .env.example .env
# Edit .env and set your BRAVE_API_KEY, TAVILY_API_KEY or FIRECRAWL_API_KEY
docker compose up -d
```

The service will be available at `http://localhost:8080`.

## API

### POST /search

```bash
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world", "type": "web", "count": 5}'
```

Response:

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

### Other Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/providers` | List available providers |
| GET | `/usage/firecrawl` | Firecrawl credit usage; returns `available=false` with null credit fields when `FIRECRAWL_API_KEY` is unset |
| GET | `/usage/firecrawl/historical?by_api_key=true` | Historical Firecrawl credit usage; API keys are masked when grouped by key |
| DELETE | `/cache` | Flush cache |

## MCP Server

The service exposes MCP tools at `/mcp` for AI agents:

- `search` — General web/news/image search
- `search_news` — News search shortcut
- `search_images` — Image search shortcut

Claude Code integration:

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

## Configuration

See [.env.example](.env.example) for all available options.

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARCH_PROVIDER` | `brave` | Active search provider |
| `BRAVE_API_KEY` | — | Brave Search API key |
| `TAVILY_API_KEY` | — | Tavily Search API key |
| `FIRECRAWL_API_KEY` | — | Firecrawl API key |
| `FIRECRAWL_API_URL` | `https://api.firecrawl.dev` | Firecrawl API base URL |
| `DEEPSEEK_API_KEY` | — | DeepSeek Search API key |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com` | DeepSeek API base URL |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `CACHE_TTL_WEB` | `600` | Web search cache TTL (seconds) |
| `CACHE_TTL_NEWS` | `300` | News search cache TTL (seconds) |
| `RATE_LIMIT_GLOBAL` | `40/second` | Global rate limit |
| `RATE_LIMIT_PER_IP` | `10/second` | Per-IP rate limit |

## Tech Stack

Python 3.12 / FastAPI / FastMCP / Redis / structlog / Docker Compose
