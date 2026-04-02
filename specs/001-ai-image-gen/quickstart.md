# Quickstart: Image Service

## Prerequisites

- Docker and Docker Compose installed
- An OpenRouter API key (get one at https://openrouter.ai/keys)

## Setup

1. Clone the repository and enter the project directory:

```bash
cd image-service
```

2. Create your environment file:

```bash
cp .env.example .env
```

3. Edit `.env` and set your OpenRouter API key:

```
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

4. Start the service:

```bash
docker compose up -d
```

The service is now running at `http://localhost:8090`.

## Usage

### Generate an image (REST API)

```bash
curl -X POST http://localhost:8090/v1/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "A minimalist blog cover illustration of a neural network",
    "model": "google/gemini-2.0-flash-exp:free",
    "aspect_ratio": "16:9"
  }'
```

Response:

```json
{
  "id": "a1b2c3d4-...",
  "image_url": "http://localhost:8090/static/images/abc123.png",
  "model": "google/gemini-2.0-flash-exp:free",
  "cached": false,
  "created_at": "2026-04-01T12:00:00Z"
}
```

### List available models

```bash
curl http://localhost:8090/v1/models
```

### Check service health

```bash
curl http://localhost:8090/v1/health
```

### Use via MCP (Claude Desktop / AI assistant)

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "image-service": {
      "url": "http://localhost:8090/mcp"
    }
  }
}
```

Then ask your AI assistant:
> "Generate a blog cover image showing a distributed system architecture"

The assistant will use the `generate_image` tool and return the image URL.

## Configuration

All settings are configured via environment variables. See `.env.example`
for the full list with defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| OPENROUTER_API_KEY | (required) | Your OpenRouter API key |
| DEFAULT_MODEL | google/gemini-2.0-flash-exp:free | Default generation model |
| REDIS_URL | redis://redis:6379/0 | Redis connection URL |
| PORT | 8090 | Service port |
| RATE_LIMIT_GLOBAL | 20 | Max generation requests per minute (global) |
| RATE_LIMIT_PER_IP | 5 | Max generation requests per minute (per IP) |
| CACHE_TTL_SECONDS | 604800 | Cache duration in seconds (default 7 days) |

## Stopping

```bash
docker compose down
```

To also remove stored images:

```bash
docker compose down -v
```
