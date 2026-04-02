# Feature Specification: AI Image Generation Middleware Service

**Feature Branch**: `001-ai-image-gen`
**Created**: 2026-04-01
**Status**: Draft
**Input**: User description: "构建 AI 图片生成中间件服务 image-service。通过 OpenRouter API 统一接入多种图片生成模型（Gemini、Flux、GPT-5 Image），提供 REST API 和 MCP Server 两种调用方式。核心场景：写博客时生成封面图、配图、概念图。"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Generate an Image via REST API (Priority: P1)

A blogger is writing a new post and needs a cover image. They send a
request to the image generation endpoint with a text prompt describing
the desired image (e.g., "A minimalist illustration of a distributed
system architecture"), optionally specifying a model, aspect ratio,
dimensions, and style. The service forwards the request to the
upstream generation provider, stores the resulting image locally, and
returns a URL pointing to the served file. If the same prompt and
model combination has been requested before, the service returns the
previously generated image immediately without calling the upstream
provider again.

**Why this priority**: This is the core value proposition — generating
images from text. Without this, no other feature matters. The REST API
is the primary integration point for blog tooling and scripts.

**Independent Test**: Can be fully tested by sending a POST request
with a prompt and verifying that an image URL is returned and the
image file is accessible. Delivers immediate value: text-to-image
generation.

**Acceptance Scenarios**:

1. **Given** the service is running and configured with a valid
   upstream API key, **When** a user sends a POST request with a text
   prompt, **Then** the service returns a response containing the
   generated image URL and the image is accessible at that URL.
2. **Given** an image was previously generated for prompt "sunset over
   mountains" with model "gemini", **When** a user sends the same
   prompt and model combination, **Then** the service returns the
   cached image URL without calling the upstream provider, and
   response time is significantly faster.
3. **Given** a user specifies an aspect ratio of 16:9 and a style of
   "watercolor", **When** the generation request is processed,
   **Then** the resulting image reflects the requested parameters.
4. **Given** the upstream provider returns an error, **When** a user
   sends a generation request, **Then** the service returns a
   meaningful error message with an appropriate status code.

---

### User Story 2 - Generate an Image via MCP Server (Priority: P2)

A blogger is using an AI assistant (e.g., Claude) that supports MCP
tool calling. They ask the assistant to generate a blog illustration.
The assistant invokes the `generate_image` MCP tool with the prompt
and optional parameters. The MCP server processes the request
identically to the REST API (including caching) and returns the image
URL to the assistant, which presents it to the user. The user can
also ask the assistant to list available models via the `list_models`
tool.

**Why this priority**: The MCP interface is the second access method,
enabling AI-native workflows. It reuses the same generation logic as
P1 but exposes it through MCP tool definitions.

**Independent Test**: Can be tested by invoking the `generate_image`
MCP tool with a prompt and verifying that an image URL is returned.
The `list_models` tool can be tested by invoking it and verifying a
list of models is returned.

**Acceptance Scenarios**:

1. **Given** the MCP server is running, **When** a client invokes the
   `generate_image` tool with a text prompt, **Then** the server
   returns a response containing the generated image URL.
2. **Given** the MCP server is running, **When** a client invokes the
   `list_models` tool, **Then** the server returns a list of available
   image generation models with their capabilities.
3. **Given** an image was previously generated via the REST API,
   **When** the same prompt and model are requested via MCP,
   **Then** the cached image is returned (cache is shared across
   interfaces).

---

### User Story 3 - Browse Available Models (Priority: P2)

A blogger wants to know which image generation models are available
before choosing one. They call the models listing endpoint and receive
a list of supported models with metadata (name, provider, supported
features). This helps them pick the best model for their use case
(e.g., photorealistic vs. illustration style).

**Why this priority**: Model discovery is essential for informed usage
but not blocking for basic generation (a default model can be used).
Shares priority with MCP since both are secondary access patterns.

**Independent Test**: Can be tested by calling the models endpoint and
verifying the response contains a list of model objects with expected
fields.

**Acceptance Scenarios**:

1. **Given** the service is running, **When** a user sends a GET
   request to the models endpoint, **Then** the service returns a
   list of available models with name and provider information.
2. **Given** multiple models are configured, **When** the models list
   is requested, **Then** all configured models appear in the
   response.

---

### User Story 4 - Rate Limiting and Abuse Prevention (Priority: P3)

The service enforces rate limits to prevent abuse and control upstream
API costs. A global limit caps total generation requests across all
users, and a per-IP limit caps requests from any single source. When
a limit is exceeded, the service returns a clear error indicating the
limit and when the user can retry.

**Why this priority**: Important for production safety but not needed
for initial functionality. Can be layered on after core generation
works.

**Independent Test**: Can be tested by sending requests exceeding the
configured limits and verifying that rate limit errors are returned
with appropriate retry-after information.

**Acceptance Scenarios**:

1. **Given** the global rate limit is 20 requests per minute, **When**
   the 21st request arrives within the same minute, **Then** the
   service returns a rate limit exceeded error with retry information.
2. **Given** the per-IP rate limit is 5 requests per minute, **When**
   the same IP sends a 6th request within one minute, **Then** the
   service returns a rate limit exceeded error.
3. **Given** a rate limit was exceeded, **When** the limit window
   resets, **Then** subsequent requests are processed normally.

---

### User Story 5 - Health Monitoring (Priority: P3)

An operations team or monitoring system checks whether the service is
running and its dependencies (upstream API connectivity, cache
availability) are healthy. They call the health endpoint and receive
a status summary.

**Why this priority**: Essential for production operations but does
not affect core user functionality.

**Independent Test**: Can be tested by calling the health endpoint and
verifying a status response is returned indicating service health.

**Acceptance Scenarios**:

1. **Given** the service and all dependencies are healthy, **When** a
   health check request is sent, **Then** the service returns a
   healthy status.
2. **Given** the cache is unavailable, **When** a health check
   request is sent, **Then** the service returns a degraded status
   indicating the cache issue.

---

### Edge Cases

- What happens when the upstream provider is down or unreachable for
  an extended period?
- What happens when the local storage disk is full and a new image
  cannot be saved?
- What happens when a user submits an extremely long prompt that
  exceeds upstream provider limits?
- What happens when the cache becomes unavailable mid-request (image
  generated but cache write fails)?
- What happens when multiple concurrent requests arrive with the same
  prompt and model (thundering herd)?
- What happens when a user specifies an invalid or unsupported model
  name?
- What happens when a user requests an unsupported aspect ratio or
  dimension combination?
- What happens when the generated image file is corrupted or
  truncated during download from the upstream provider?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST accept text prompts and generate images via
  an upstream image generation provider (OpenRouter API).
- **FR-002**: System MUST support specifying the generation model,
  aspect ratio, dimensions, and style as optional parameters. When
  not specified, sensible defaults MUST be applied.
- **FR-003**: System MUST store generated images as local files and
  serve them via a static file endpoint.
- **FR-004**: System MUST cache generation results keyed by the
  combination of prompt and model. Identical requests MUST return
  the cached image without calling the upstream provider.
- **FR-005**: System MUST expose a REST API with the following
  endpoints:
  - `POST /generate` — submit a text-to-image generation request
  - `GET /models` — list available image generation models
  - `GET /health` — service health check
- **FR-006**: System MUST expose an MCP Server with two tools:
  - `generate_image` — generate an image from a text prompt
  - `list_models` — list available image generation models
- **FR-007**: The MCP Server and REST API MUST share the same
  generation logic, caching layer, and stored images.
- **FR-008**: System MUST enforce rate limiting: 20 requests per
  minute globally and 5 requests per minute per IP address, applied
  to the generation endpoint.
- **FR-009**: System MUST return meaningful error messages with
  appropriate status codes when generation fails, rate limits are
  exceeded, or invalid parameters are provided.
- **FR-010**: System MUST be deployable via Docker Compose, exposing
  the service on port 8090.
- **FR-011**: System MUST support multiple image generation models
  including Gemini, Flux, and GPT-5 Image via the upstream provider.
- **FR-012**: The generation response MUST include at minimum: the
  image URL, the model used, and a unique identifier for the
  generation.
- **FR-013**: Static file serving MUST allow direct access to
  generated images via their URL without authentication.

### Key Entities

- **Generation Request**: Represents a user's request to generate an
  image. Attributes: text prompt, model (optional, defaults to a
  configured default), aspect ratio (optional), dimensions (optional),
  style (optional), requester IP.
- **Generation Result**: Represents a completed generation. Attributes:
  unique identifier, prompt, model used, image file path, image URL,
  creation timestamp, whether served from cache.
- **Model**: Represents an available image generation model. Attributes:
  identifier, display name, provider, supported features (aspect
  ratios, styles, max dimensions).
- **Rate Limit State**: Tracks request counts per time window.
  Attributes: window key (global or IP-based), request count, window
  start time, window duration.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can generate an image from a text prompt and
  receive a usable image URL in a single request-response cycle.
- **SC-002**: Repeated requests with the same prompt and model return
  the cached image in under 1 second end-to-end.
- **SC-003**: The service supports at least 3 different image
  generation models simultaneously.
- **SC-004**: Both the REST API and MCP Server produce identical
  results for the same inputs (shared generation and caching).
- **SC-005**: Rate limiting correctly blocks requests exceeding 20 per
  minute globally and 5 per minute per IP, with clear feedback to
  the user.
- **SC-006**: The service starts and operates correctly via a single
  deployment command with no manual configuration steps beyond
  providing environment credentials.
- **SC-007**: Generated images remain accessible via their URLs for
  the lifetime of the service deployment.
- **SC-008**: The health endpoint reflects actual dependency status
  within 5 seconds of a dependency becoming unavailable.

## Assumptions

- The upstream image generation provider (OpenRouter) is a third-party
  service whose availability is outside our control; the service
  handles upstream failures gracefully but does not guarantee
  generation uptime.
- Users provide their own upstream API key via environment
  configuration; the service does not manage user accounts or
  authentication for its own endpoints.
- Image generation is a synchronous operation from the user's
  perspective (the service waits for the upstream provider to
  complete before responding). If upstream latency becomes
  problematic, async patterns can be introduced in a future
  iteration.
- Local file storage is sufficient for the expected volume (blog
  usage); cloud storage (S3, etc.) is out of scope for v1.
- The service runs as a single instance behind Docker Compose; horizontal
  scaling and load balancing are out of scope for v1.
- MCP Server and REST API run within the same process or deployment
  unit, sharing in-memory and cache state.
- Image files are stored in a common format (PNG or JPEG) as returned
  by the upstream provider; format conversion is out of scope.
- The default model is configurable via environment variable; if not
  set, the first model in the configured list is used.
