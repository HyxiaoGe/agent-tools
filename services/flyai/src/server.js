import { createHash } from "node:crypto";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { TextDecoder } from "node:util";

import { runFlyaiCli } from "./flyai-runner.js";
import { ConcurrencyGate } from "./gate.js";
import { handleMcpRequest } from "./mcp.js";
import { projectProviderResponse } from "./projector.js";
import { buildCliInvocation, validateSearchRequest, validateUserScope } from "./schema.js";
import { parseStrictJson } from "./strict-json.js";
import { chinaToday } from "./travel.js";
import { UsageCounter, isDay } from "./usage.js";

const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_MCP_REQUEST_BYTES = 64 * 1024;
const ERROR_STATUS = Object.freeze({
  not_found: 404,
  method_not_allowed: 405,
  invalid_request: 400,
  concurrency_limited: 429,
  provider_timeout: 504,
  provider_output_too_large: 502,
  invalid_provider_response: 502,
  provider_error: 502,
  credential_unavailable: 503,
  request_aborted: 499,
  internal_error: 500,
});

// 这些错误在启动 CLI 之前就返回，没有调用 FlyAI：带上 x-upstream-calls: 0，网关据此不计入每日上限。
const REJECTED_BEFORE_PROVIDER = new Set([
  "not_found",
  "method_not_allowed",
  "invalid_request",
  "concurrency_limited",
  "credential_unavailable",
]);

// 并发隔离的 scope 来自网关重写后的产品 / 用户头：同一用户同时只跑一个 CLI 进程。
// 只保留不可逆摘要，用户标识不进入内存以外的任何地方。
function resolveUserScope(headers) {
  const app = validateUserScope(headers["x-caller-app"]);
  const user = headers["x-caller-user"];
  if (user === undefined) return createHash("sha256").update(app).digest("hex");
  // 用户标识的格式由网关校验，这里只限长度，整体只保留摘要。
  if (typeof user !== "string" || !user || user.length > 128) throw new Error("invalid_request");
  return createHash("sha256").update(`${app}\n${user}`).digest("hex");
}

function sendJson(response, statusCode, payload, extraHeaders = {}) {
  if (response.destroyed || response.writableEnded) return;
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
    "content-type": "application/json; charset=utf-8",
    "x-content-type-options": "nosniff",
    ...extraHeaders,
  });
  response.end(body);
}

function sendError(response, code) {
  const safeCode = code in ERROR_STATUS ? code : "internal_error";
  const headers = REJECTED_BEFORE_PROVIDER.has(safeCode) ? { "x-upstream-calls": "0" } : {};
  sendJson(response, ERROR_STATUS[safeCode], { error_code: safeCode }, headers);
}

async function readRequestBody(request) {
  const contentLength = Number(request.headers["content-length"] ?? 0);
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > MAX_REQUEST_BYTES) {
    throw new Error("invalid_request");
  }
  const chunks = [];
  let size = 0;
  let exceeded = false;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_REQUEST_BYTES) {
      exceeded = true;
      continue;
    }
    chunks.push(chunk);
  }
  if (exceeded) throw new Error("invalid_request");
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    throw new Error("invalid_request");
  }
  try {
    return parseStrictJson(text, { maxDepth: 12, maxNodes: 2_000, maxStringChars: 2_048 });
  } catch {
    throw new Error("invalid_request");
  }
}

function resolveRoute(pathname) {
  if (pathname === "/v1/search/flights") return "flight";
  if (pathname === "/v1/search/trains") return "train";
  return null;
}

export function createAdapterServer({
  apiKey,
  runner,
  runnerOptions = {},
  clock = () => new Date(),
  homeDir = "/tmp",
  deviceId,
  usage = new UsageCounter({ clock }),
  mcpQueueTimeoutMs,
  log = (line) => process.stderr.write(`${line}\n`),
} = {}) {
  if (typeof apiKey !== "string" || !apiKey.trim()) {
    throw new Error("FLYAI_API_KEY 必须配置");
  }
  if (!path.isAbsolute(homeDir)) {
    throw new Error("FlyAI HOME 必须是绝对路径");
  }

  // REST 和 MCP 共用一个闸门和一份计数
  const gate = new ConcurrencyGate(2);
  const runCli =
    runner ??
    (({ command, argv, signal }) =>
      runFlyaiCli({
        ...runnerOptions,
        command,
        argv,
        signal,
        apiKey,
        homeDir,
        deviceId,
      }));
  const execute = async (invocation) => {
    try {
      const payload = await runCli(invocation);
      usage.record(null);
      return payload;
    } catch (error) {
      usage.record(error?.code ?? "internal_error");
      throw error;
    }
  };

  const server = http.createServer(async (request, response) => {
    let parsedUrl;
    try {
      parsedUrl = new URL(request.url ?? "/", "http://adapter.internal");
    } catch {
      sendError(response, "not_found");
      return;
    }

    if (parsedUrl.pathname === "/health") {
      if (request.method !== "GET") {
        sendError(response, "method_not_allowed");
        return;
      }
      if (parsedUrl.search) {
        sendError(response, "invalid_request");
        return;
      }
      sendJson(response, 200, { status: "healthy" });
      return;
    }

    if (parsedUrl.pathname === "/usage/daily") {
      if (request.method !== "GET") {
        sendError(response, "method_not_allowed");
        return;
      }
      const day = parsedUrl.searchParams.get("day") ?? chinaToday(clock());
      if (!isDay(day) || [...parsedUrl.searchParams.keys()].some((key) => key !== "day")) {
        sendError(response, "invalid_request");
        return;
      }
      sendJson(response, 200, usage.summary(day));
      return;
    }

    if (parsedUrl.pathname === "/mcp") {
      // 无状态：没有 SSE 流和会话可取、可删
      if (request.method !== "POST") {
        sendError(response, "method_not_allowed");
        return;
      }
      // 请求体由 SDK 自己读，先按长度挡掉过大的（网关转发时带 content-length）
      const contentLength = Number(request.headers["content-length"]);
      if (!Number.isSafeInteger(contentLength) || contentLength < 0 || contentLength > MAX_MCP_REQUEST_BYTES) {
        sendError(response, "invalid_request");
        return;
      }
      let scope;
      try {
        scope = resolveUserScope(request.headers);
      } catch {
        sendError(response, "invalid_request");
        return;
      }
      try {
        await handleMcpRequest(
          request,
          response,
          { gate, scope, execute, clock, log, queueTimeoutMs: mcpQueueTimeoutMs },
          { maxBytes: MAX_MCP_REQUEST_BYTES },
        );
      } catch (error) {
        log(`flyai_mcp_request_failed ${error?.stack ?? error}`);
        sendError(response, "internal_error");
      } finally {
        server.emit("flyai-request-settled");
      }
      return;
    }

    const kind = resolveRoute(parsedUrl.pathname);
    if (kind === null) {
      sendError(response, "not_found");
      return;
    }
    if (request.method !== "POST") {
      sendError(response, "method_not_allowed");
      return;
    }
    if (parsedUrl.search) {
      sendError(response, "invalid_request");
      return;
    }
    if (!(request.headers["content-type"] ?? "").toLowerCase().startsWith("application/json")) {
      sendError(response, "invalid_request");
      return;
    }

    let userScope;
    let normalizedRequest;
    try {
      userScope = resolveUserScope(request.headers);
      normalizedRequest = validateSearchRequest(await readRequestBody(request), kind);
    } catch {
      sendError(response, "invalid_request");
      return;
    }

    if (!gate.tryAcquire(userScope)) {
      sendError(response, "concurrency_limited");
      return;
    }
    const controller = new AbortController();
    const cancel = () => controller.abort();
    const cancelOnClose = () => {
      if (!response.writableEnded) cancel();
    };
    request.once("aborted", cancel);
    response.once("close", cancelOnClose);
    if (request.aborted || response.destroyed) cancel();
    try {
      const invocation = buildCliInvocation(normalizedRequest, kind);
      const providerPayload = await execute({ ...invocation, signal: controller.signal });
      const items = projectProviderResponse(providerPayload, normalizedRequest, kind);
      sendJson(response, 200, {
        observed_at: clock().toISOString(),
        request: normalizedRequest,
        items,
      });
    } catch (error) {
      sendError(response, error?.code ?? "provider_error");
    } finally {
      request.off("aborted", cancel);
      response.off("close", cancelOnClose);
      gate.release(userScope);
      // HTTP 客户端先感知 abort，服务端仍需等待 CLI 进程组和临时目录清理。
      // 该事件只表示服务端本次业务请求已完全收尾，不携带任何查询或凭证数据。
      server.emit("flyai-request-settled");
    }
  });
  return server;
}
