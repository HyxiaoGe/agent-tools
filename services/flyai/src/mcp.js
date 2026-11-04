// MCP 工具 search_flights、search_trains：产品经网关 /flyai/mcp 给自己的模型用，参数和结果规矩在 travel.js。
//
// 说明（英文，模型看的）两个产品共用，参考了 Fusion 的说明（runtime_prompts.toml [flyai] 325-329），按这里的结果重写，不是照抄：
// 日期要用户的话就能定、定不了先问；只看返回的字段，不说余票、准点、退改、行李、登机口、站台；比较只在这次结果里；
// 价格是查询时的参考价，订之前再核对；limitations 里的事不暗示。
// 这里新写的：最多取飞猪 10 条、时刻是当地时间（国际航线不换算）、travel_class 是价格对应的舱位、
// arrival.day_offset、高铁没覆盖的提示。
// 产品专有的话由产品接在后面：卡片怎么显示、回答风格、和地图工具怎么配合（接站、换乘）。所以这里不点名别的工具、不提卡片。
//
// 结果：structuredContent 是整份（给页面画卡片），文字是给模型的那份 JSON（去掉 ok 和预订链接，免得它把链接贴给用户）。
// 失败：structuredContent 是 {ok:false, error:{code, message, retryable}}，文字是给模型的说明；isError 保持 false，和 maps 一样。
// 只有 busy 是 retryable。参数没过 schema 的，SDK 自己回 isError=true，没有调用飞猪。
//
// 协议：MCP SDK v2 的 createMcpHandler，2026-07-28（新）和 2025 年那几版（旧，无状态）一起接。
// 新客户端（灵稿的 Python mcp 2.3）先按新协议连，旧 SDK 不认会回 400、再走老握手，一次调用要四个请求，现在两个。

import { McpServer, createMcpHandler } from "@modelcontextprotocol/server";
import * as z from "zod";

import {
  CABIN_CLASSES,
  MAX_LIMIT,
  SEAT_CLASSES,
  SORT_TYPES,
  TRAIN_CATEGORIES,
  ToolFailure,
  buildToolInvocation,
  chinaIso,
  normalizeToolArgs,
  shapeToolResult,
} from "./travel.js";

export const FLIGHT_TOOL = "search_flights";
export const TRAIN_TOOL = "search_trains";
// 排队等并发槽最多等多久；加上 CLI 最长 20 秒，在网关给 flyai 的 30 秒之内
export const QUEUE_TIMEOUT_MS = 6_000;

const DATE_RULES = `- Call only when the user's own words determine the origin, the destination and one exact departure date. If several dates are plausible (for example "next weekend" or "around the 10th"), ask which date first and do not call any tool before the user answers; never pick a date for the user. Do not infer a year the user did not give beyond the nearest upcoming one.
- departure_date is YYYY-MM-DD in China time and cannot be in the past.`;

const FILTER_RULES = `- departure_after_hour, departure_before_hour and arrival_before_hour are whole hours in local time: departure_after_hour=8 keeps departures at 08:00 or later, departure_before_hour=12 keeps departures before 12:00, arrival_before_hour=18 keeps services arriving before 18:00 on the departure date. Pass them only when the user states such a time limit.
- max_price_yuan only when the user states a budget per ticket.
- sort_by: price_asc when the user asks for the cheapest, duration_asc for the fastest, departure_asc for the earliest; otherwise omit it (recommended order).`;

const USAGE_RULES = (noun, extras) => `Using the result:
- Treat only returned fields as facts: every ${noun} number, ${extras.places}, time, duration, class and price must come from the result. Do not add or infer seat availability, ${extras.unknowns}, change or refund rules, or real-time prices.
- One query fetches at most 10 services from the provider and returns up to 5 of them; it is not a complete timetable. Keep comparisons such as cheapest, fastest or earliest within the returned services and say they cover this query's results; do not claim no other service exists.
- Times are local times at each place. Use duration_minutes for travel time; do not compute durations from the times or convert time zones.
- You may calculate direct differences between returned values of the same kind, such as price or duration differences; do not introduce values that were not returned.
- Prices are reference prices at observed_at and change; when the user is choosing what to book, say to check again before booking.
- limitations lists what this query did not cover. Never assert or imply those items in any wording. If the user asks about them, say this query cannot confirm them.

On failure the result has ok=false and says what to do next. In every case, do not fabricate ${noun} facts.`;

const SERVICE_FIELDS = (carrierField) => `- services: each has option_id, number, ${carrierField}, departure and arrival (city, station, terminal when known, and time as YYYY-MM-DDTHH:MM local time), arrival.day_offset (days after the departure date; 1 means the next day), duration_minutes, travel_class (the class the price is for) and price_yuan (reference price per ticket at query time). A service may lack fields another service has.`;

const FLIGHT_DESCRIPTION = `Search one-way direct flights between two places on one date and return up to ${MAX_LIMIT} services with reference prices at query time. It does not check seat availability, on-time performance, change or refund rules, baggage or boarding gates, and it does not book.

Arguments:
${DATE_RULES}
- origin and destination are city or airport names as the user gave them, such as 上海 or 浦东机场.
- cabin_class only when the user names a cabin.
${FILTER_RULES}

The result is JSON:
- observed_at: when this query ran, in China time. query: the arguments as applied.
${SERVICE_FIELDS("airline")} station is the airport.
- limitations: filtered_partial_list means only part of the fetched services matched the filters, so other matching services may exist; other_class_returned means some services are priced for a cabin other than the one requested.

${USAGE_RULES("flight", {
  places: "airline, airport, terminal",
  unknowns: "on-time performance, delays, baggage, boarding gates",
})}`;

const TRAIN_DESCRIPTION = `Search one-way direct trains between two places on one date and return up to ${MAX_LIMIT} services with reference prices at query time. It does not check ticket availability, change or refund rules, ticket gates or platforms, and it does not buy tickets.

Arguments:
${DATE_RULES}
- origin and destination are city or station names as the user gave them, such as 杭州 or 杭州东站.
- train_category: high_speed returns only G, D and C trains (high-speed and bullet trains); pass it when the user asks for 高铁 or 动车. Otherwise pass all.
- seat_class only when the user names a seat or berth class.
${FILTER_RULES}

The result is JSON:
- observed_at: when this query ran, in China time. query: the arguments as applied.
${SERVICE_FIELDS("train_type (such as 高铁 or 普快)")} Overnight trains may take more than a day.
- limitations: filtered_partial_list means only part of the fetched services matched the filters, so other matching services may exist; other_class_returned means some services are priced for a class other than the one requested; high_speed_not_covered means the fetched services contained no G, D or C trains, so high-speed services on this route may exist but were not fetched; call again with train_category=high_speed if the user wants them.

${USAGE_RULES("train", {
  places: "train type, station",
  unknowns: "delays, ticket gates, platforms",
})}`;

const place = (role, example) =>
  z
    .string()
    .min(1)
    .max(80)
    .describe(`${role} as the user gave it, a city, ${example} name. 1-80 characters.`);
const hour = (min, max, text) => z.number().int().min(min).max(max).nullish().describe(text);

function commonShape(kind) {
  const stationWord = kind === "flight" ? "or airport" : "or station";
  return {
    origin: place("Origin", stationWord),
    destination: place("Destination", stationWord),
    departure_date: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/u)
      .describe("Departure date as YYYY-MM-DD, determined by the user's own words; not in the past."),
    departure_after_hour: hour(0, 23, "Optional. Keep departures at or after this whole hour, 0-23."),
    departure_before_hour: hour(1, 24, "Optional. Keep departures before this whole hour, 1-24."),
    arrival_before_hour: hour(1, 24, "Optional. Keep services arriving before this whole hour on the departure date, 1-24."),
    max_price_yuan: z.number().int().min(1).max(100_000).nullish().describe("Optional. Highest reference price per ticket in yuan."),
    sort_by: z
      .enum(Object.keys(SORT_TYPES))
      .nullish()
      .describe("Optional. recommended (default), price_asc, duration_asc or departure_asc."),
    limit: z.number().int().min(1).max(MAX_LIMIT).nullish().describe(`Optional. Number of services to return, 1-${MAX_LIMIT}. Default ${MAX_LIMIT}.`),
  };
}

const FLIGHT_SHAPE = {
  ...commonShape("flight"),
  cabin_class: z
    .enum(Object.keys(CABIN_CLASSES))
    .nullish()
    .describe("Optional. Cabin the user named: economy, premium_economy, business or first."),
};
const TRAIN_SHAPE = {
  ...commonShape("train"),
  train_category: z.enum(TRAIN_CATEGORIES).describe("high_speed for G, D and C trains only, all for every train type."),
  seat_class: z
    .enum(Object.keys(SEAT_CLASSES))
    .nullish()
    .describe(
      "Optional. Seat or berth class the user named: second_class, first_class, business_class, premium_class, hard_seat, soft_seat, hard_sleeper, soft_sleeper or no_seat.",
    ),
};

// 给日志和页面看的原因；模型看的是 modelText
const MESSAGES = {
  invalid_arguments: "The arguments are invalid.",
  origin_not_recognized: "The travel service did not recognize the origin.",
  destination_not_recognized: "The travel service did not recognize the destination.",
  no_services: "No direct services matched this query.",
  provider_no_result: "The travel service returned no result for this route and date.",
  busy: "The travel service is busy with other queries.",
  timeout: "The travel query took too long.",
  unavailable: "The travel service returned no usable result.",
};

function quoted(value) {
  return JSON.stringify(value);
}

export function modelText(error, kind, request) {
  const noun = kind === "flight" ? "flight" : "train";
  const route = request ? ` from ${quoted(request.origin)} to ${quoted(request.destination)} on ${request.departure_date}` : "";
  switch (error.code) {
    case "invalid_arguments":
      return `No result was fetched: ${error.detail}. Call again with corrected arguments only if the user's request supports it; if the date is in the past, ask the user which date they mean. Do not fabricate ${noun} facts.`;
    case "origin_not_recognized":
    case "destination_not_recognized": {
      const which = error.code === "origin_not_recognized" ? "origin" : "destination";
      const value = request ? quoted(request[which]) : `the ${which}`;
      return `No result was fetched: the travel service did not recognize the ${which} ${value}. It may be a place without its own ${kind === "flight" ? "airport" : "railway station"}, or a name the service does not know. If the conversation makes the intended city clear, call again with that city; otherwise ask the user which city to use. Do not guess a nearby city yourself or fabricate ${noun} facts.`;
    }
    case "no_services":
      if (error.extra?.filtered) {
        return `This query found direct ${noun}s${route}, but none matched the requested time, price or class limits. Tell the user that none of the services this query fetched meet those limits; this does not prove none exist, because one query fetches at most 10 services. Do not search other dates or relax the limits unless the user asks, and do not fabricate ${noun} facts.`;
      }
      return `This query returned no direct ${noun}s${route}. Tell the user this query found none; it does not prove that no service runs, and connecting services are not covered. Do not search other dates unless the user asks, and do not fabricate ${noun} facts.`;
    case "provider_no_result":
      return `The travel service returned no result${route}. This happens both when no direct ${noun} runs on this route and when the search itself failed, and the two cannot be told apart. Tell the user this query could not find direct ${noun}s for this route and date, without claiming that none exist. Do not repeat the same query, and do not fabricate ${noun} facts.`;
    case "busy":
      return `No result was fetched because the travel service is busy with other queries. You may call again once; if it is still busy, tell the user the lookup could not be completed right now. Do not fabricate ${noun} facts.`;
    default:
      // 超时、飞猪出错、返回看不懂：只说没查到，不让模型把服务状态讲给用户
      return `No usable result was fetched from the travel service. Tell the user the ${noun} lookup could not be completed and suggest trying again later. Do not give ${noun} numbers, times or prices from memory.`;
  }
}

function failureResult(error, kind, request) {
  const body = {
    code: error.code,
    message: error.detail || MESSAGES[error.code] || "The travel query failed.",
    retryable: error.code === "busy",
  };
  return {
    content: [{ type: "text", text: modelText(error, kind, request) }],
    structuredContent: { ok: false, error: body },
  };
}

// 模型看不到的字段：ok 和预订链接（卡片上有，模型不用讲、也别贴给用户）
const HIDDEN = new Set(["ok", "booking_url"]);

export function forModel(value) {
  if (Array.isArray(value)) return value.map(forModel);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !HIDDEN.has(key)).map(([key, item]) => [key, forModel(item)]));
  }
  return value;
}

function successResult(result) {
  return {
    content: [{ type: "text", text: JSON.stringify(forModel(result)) }],
    structuredContent: result,
  };
}

const RUNNER_ERRORS = {
  provider_timeout: "timeout",
  request_aborted: "unavailable",
};

/**
 * 每个 HTTP 请求建一个 MCP server（无状态），工具返回 CallToolResult。
 * context：{ gate, scope, execute, clock, onProviderCall, signal, log, queueTimeoutMs }。
 */
export function buildMcpServer(context) {
  const server = new McpServer(
    { name: "flyai", version: "1.0.0" },
    {
      instructions:
        "search_flights and search_trains look up one-way direct flights and trains on one date with reference prices; see the tool descriptions.",
    },
  );
  const annotations = { readOnlyHint: true, idempotentHint: true, openWorldHint: true };
  server.registerTool(
    FLIGHT_TOOL,
    { description: FLIGHT_DESCRIPTION, inputSchema: z.object(FLIGHT_SHAPE), annotations },
    (args, ctx) => runTool("flight", args, ctx, context),
  );
  server.registerTool(
    TRAIN_TOOL,
    { description: TRAIN_DESCRIPTION, inputSchema: z.object(TRAIN_SHAPE), annotations },
    (args, ctx) => runTool("train", args, ctx, context),
  );
  return server;
}

async function runTool(kind, args, ctx, context) {
  const { gate, scope, execute, clock, onProviderCall, signal, log, queueTimeoutMs = QUEUE_TIMEOUT_MS } = context;
  let request = null;
  const abortSignal = AbortSignal.any([signal, ctx.mcpReq?.signal].filter(Boolean));
  try {
    const now = clock();
    request = normalizeToolArgs(kind, args, now);
    if (!(await gate.acquire(scope, { timeoutMs: queueTimeoutMs, signal: abortSignal }))) {
      throw new ToolFailure("busy");
    }
    try {
      const invocation = buildToolInvocation(request, kind);
      onProviderCall();
      let payload;
      try {
        payload = await execute({ ...invocation, signal: abortSignal });
      } catch (error) {
        throw new ToolFailure(RUNNER_ERRORS[error?.code] ?? "unavailable");
      }
      return successResult(shapeToolResult(payload, request, kind, chinaIso(now)));
    } finally {
      gate.release(scope);
    }
  } catch (error) {
    if (error instanceof ToolFailure) {
      // 地名、日期不进日志
      log(`flyai_tool_failed tool=${kind} code=${error.code}`);
      return failureResult(error, kind, request);
    }
    log(`flyai_tool_crashed tool=${kind} ${error?.stack ?? error}`);
    return failureResult(new ToolFailure("unavailable"), kind, request);
  }
}

// 转给 SDK 时不带的头：连接本身的，以及由请求体重新算的长度
const HOP_HEADERS = new Set(["host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"]);

async function readBody(request, maxBytes) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error("mcp request body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function webHeaders(nodeHeaders) {
  const headers = new Headers();
  for (const [name, value] of Object.entries(nodeHeaders)) {
    if (value === undefined || HOP_HEADERS.has(name)) continue;
    for (const item of Array.isArray(value) ? value : [value]) headers.append(name, item);
  }
  return headers;
}

/**
 * 处理一次 POST /mcp（请求体大小由调用方按 content-length 先挡过）。
 * 等 SDK 把整个响应给出来（结果都很小）才写响应头，所以能照实标出这次有没有启动 CLI：
 * 没启动的（握手、列工具、参数不对、排队超时）带 x-upstream-calls: 0，网关据此不算进每日上限。
 * 响应格式由 SDK 定：新协议是一块 JSON（工具不发中途通知），旧协议的无状态那一路是一条 SSE 消息，客户端两种都认。
 */
export async function handleMcpRequest(request, response, context, { maxBytes = 64 * 1024 } = {}) {
  let providerCalled = false;
  const controller = new AbortController();
  const abortOnClose = () => {
    if (!response.writableEnded) controller.abort();
  };
  response.once("close", abortOnClose);

  // 每个请求一个 handler：onProviderCall、signal 是这个请求自己的
  const handler = createMcpHandler(() =>
    buildMcpServer({
      ...context,
      signal: controller.signal,
      onProviderCall: () => {
        providerCalled = true;
      },
    }),
  );
  try {
    const body = await readBody(request, maxBytes);
    const reply = await handler.fetch(
      new Request(new URL(request.url, "http://flyai"), {
        method: request.method,
        headers: webHeaders(request.headers),
        body,
        signal: controller.signal,
      }),
    );
    const payload = Buffer.from(await reply.arrayBuffer());
    const headers = Object.fromEntries(reply.headers);
    if (!providerCalled) headers["x-upstream-calls"] = "0";
    response.writeHead(reply.status, headers);
    response.end(payload);
  } finally {
    response.off("close", abortOnClose);
    await handler.close().catch(() => {});
  }
}
