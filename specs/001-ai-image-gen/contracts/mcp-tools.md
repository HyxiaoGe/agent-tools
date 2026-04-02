# MCP Tools Contract: Image Service

**Transport**: Streamable HTTP mounted at `/mcp`
**Server name**: `image-service`

## Tool: generate_image

Generate an image from a text prompt via OpenRouter.

### Input Schema

```json
{
  "type": "object",
  "properties": {
    "prompt": {
      "type": "string",
      "description": "Text description of the image to generate"
    },
    "model": {
      "type": "string",
      "description": "OpenRouter model ID (optional, uses default if omitted)"
    },
    "aspect_ratio": {
      "type": "string",
      "description": "Aspect ratio, e.g. '16:9', '4:3', '1:1' (default: '1:1')"
    },
    "size": {
      "type": "string",
      "description": "Output dimensions as 'WxH', e.g. '1024x1024' (default: '1024x1024')"
    },
    "style": {
      "type": "string",
      "description": "Style hint, e.g. 'watercolor', 'photorealistic' (optional)"
    }
  },
  "required": ["prompt"]
}
```

### Successful Response

Returns a text content block with the image URL and metadata:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Generated image: http://localhost:8090/static/images/abc123.png\nModel: google/gemini-2.0-flash-exp:free\nCached: false"
    },
    {
      "type": "image",
      "data": "<base64-encoded-image>",
      "mimeType": "image/png"
    }
  ]
}
```

Note: The tool returns both a text summary with the permanent URL and
an inline image preview. The text URL is the canonical reference for
embedding in blog posts.

### Error Response

```json
{
  "content": [
    {
      "type": "text",
      "text": "Error generating image: upstream provider returned an error"
    }
  ],
  "isError": true
}
```

---

## Tool: list_models

List available image generation models.

### Input Schema

```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

### Successful Response

```json
{
  "content": [
    {
      "type": "text",
      "text": "Available models:\n\n1. Gemini 2.0 Flash (google/gemini-2.0-flash-exp:free) — Google\n2. Flux 1.1 Pro (black-forest-labs/flux-1.1-pro) — Black Forest Labs\n3. GPT-5 Image (openai/gpt-5) — OpenAI"
    }
  ]
}
```

---

## Shared Behavior

- Both MCP tools use the same generation service and Redis cache as
  the REST API endpoints.
- Cache hits via MCP return the same stored image as REST API cache
  hits.
- Rate limiting applies to `generate_image` tool invocations with
  the same limits as `POST /v1/generate`. The client IP is extracted
  from the MCP transport connection.
- Errors follow MCP protocol conventions (`isError: true` in the tool
  response).
