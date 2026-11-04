// MCP 工具 search_flights / search_trains 的参数规整、调用 CLI 的参数、结果投影。
// REST 接口（server.js 的 /v1/search/*）保持原样，这里是 MCP 这一路的契约，规矩来自 11-04 录的真实样本
// （test/fixtures/）和 Fusion 接入时踩过的坑（04 文档第十二节）。

import { createHash } from "node:crypto";

import { extractItemList, isRecord, placeMatches, projectBookingUrl, projectDuration, projectPrice, safeString } from "./projector.js";

export const MAX_LIMIT = 5;
// CLI 每次向飞猪固定要 10 条（没有改条数的参数），满页时就是 10 条
export const PROVIDER_PAGE_SIZE = 10;

// 舱位 / 座席：模型传英文代号，换成飞猪认的中文名。aliases 是飞猪返回时算作同一种的名字。
// 11-04 样本：查头等舱飞猪返回的全是公务舱，所以不按舱位丢班次，只标出 other_class_returned。
export const CABIN_CLASSES = Object.freeze({
  economy: { provider: "经济舱", aliases: ["经济舱"] },
  premium_economy: { provider: "超级经济舱", aliases: ["超级经济舱"] },
  business: { provider: "公务舱", aliases: ["公务舱", "商务舱"] },
  first: { provider: "头等舱", aliases: ["头等舱"] },
});
export const SEAT_CLASSES = Object.freeze({
  second_class: { provider: "二等座", aliases: ["二等座"] },
  first_class: { provider: "一等座", aliases: ["一等座"] },
  business_class: { provider: "商务座", aliases: ["商务座"] },
  premium_class: { provider: "特等座", aliases: ["特等座"] },
  hard_seat: { provider: "硬座", aliases: ["硬座"] },
  soft_seat: { provider: "软座", aliases: ["软座"] },
  hard_sleeper: { provider: "硬卧", aliases: ["硬卧"] },
  soft_sleeper: { provider: "软卧", aliases: ["软卧"] },
  no_seat: { provider: "无座", aliases: ["无座"] },
});
export const SORT_TYPES = Object.freeze({ recommended: "2", price_asc: "3", duration_asc: "4", departure_asc: "6" });
export const TRAIN_CATEGORIES = Object.freeze(["high_speed", "all"]);

// G / D / C 字头是高铁、动车、城际（Fusion c843e90e 起按车次字头筛）
const HIGH_SPEED_NUMBER = /^[GDC]\d/iu;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/u;
const LOCAL_TIME = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:Z|[+-]\d{2}:\d{2})?$/u;

export class ToolFailure extends Error {
  constructor(code, detail = "", extra = {}) {
    super(code);
    this.name = "ToolFailure";
    this.code = code;
    this.detail = detail;
    this.extra = extra;
  }
}

function invalid(detail) {
  throw new ToolFailure("invalid_arguments", detail);
}

/** 北京时间的今天 YYYY-MM-DD。 */
export function chinaToday(now) {
  return new Date(now.getTime() + 8 * 3_600_000).toISOString().slice(0, 10);
}

/** 北京时间、带 +08:00 的 ISO 时刻（查询时刻给模型看，避免把 UTC 念成本地时间，Fusion 4919286d）。 */
export function chinaIso(now) {
  return `${new Date(now.getTime() + 8 * 3_600_000).toISOString().slice(0, 19)}+08:00`;
}

function placeText(value, field) {
  if (typeof value !== "string") invalid(`${field} must be a place name`);
  const text = value.trim();
  if (!text || text.length > 80 || text.startsWith("-") || /[\u0000-\u001f\u007f]/u.test(text)) {
    invalid(`${field} must be a place name of 1-80 characters`);
  }
  return text;
}

function validDate(value, today) {
  const match = typeof value === "string" ? value.match(DATE_PATTERN) : null;
  if (!match) invalid("departure_date must be YYYY-MM-DD");
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  if (date.toISOString().slice(0, 10) !== value) invalid("departure_date is not a real calendar date");
  // 飞猪对过去的日期回“出发日期非法”，不花一次调用去问
  if (value < today) invalid(`departure_date ${value} is before today (${today}, China time)`);
  return value;
}

/** MCP 参数（已过 SDK 的 schema 校验）再做跨字段检查，规整成内部请求。 */
export function normalizeToolArgs(kind, args, now) {
  const request = {
    origin: placeText(args.origin, "origin"),
    destination: placeText(args.destination, "destination"),
    departure_date: validDate(args.departure_date, chinaToday(now)),
    sort_by: args.sort_by ?? "recommended",
    limit: args.limit ?? MAX_LIMIT,
  };
  if (request.origin === request.destination) invalid("origin and destination must differ");
  if (kind === "flight" && args.cabin_class != null) request.cabin_class = args.cabin_class;
  if (kind === "train") {
    if (args.seat_class != null) request.seat_class = args.seat_class;
    request.train_category = args.train_category ?? "all";
  }
  // 模型有时把没用到的可选参数传成 null，当作没传
  for (const field of ["max_price_yuan", "departure_after_hour", "departure_before_hour", "arrival_before_hour"]) {
    if (args[field] !== undefined && args[field] !== null) request[field] = args[field];
  }
  const after = request.departure_after_hour;
  if (after !== undefined && request.departure_before_hour !== undefined && after >= request.departure_before_hour) {
    invalid("departure_after_hour must be earlier than departure_before_hour");
  }
  if (after !== undefined && request.arrival_before_hour !== undefined && after >= request.arrival_before_hour) {
    invalid("departure_after_hour must be earlier than arrival_before_hour");
  }
  return request;
}

function classSpec(request, kind) {
  if (kind === "flight") return request.cabin_class ? CABIN_CLASSES[request.cabin_class] : null;
  return request.seat_class ? SEAT_CLASSES[request.seat_class] : null;
}

/** 调 CLI 的参数。筛选尽量交给飞猪，让它那 10 条就是符合条件的（先筛后截）。 */
export function buildToolInvocation(request, kind) {
  const argv = [
    "--origin",
    request.origin,
    "--destination",
    request.destination,
    "--dep-date",
    request.departure_date,
    "--journey-type",
    "1",
  ];
  let providerClass = classSpec(request, kind)?.provider;
  // 只要高铁：CLI 没有车型参数。11-04 样本：北京→哈尔滨默认 10 条全是普速车，带上二等座就全是 G 字头；
  // 二等座只有动车组有。用户自己指定了座席就用用户的。
  if (kind === "train" && request.train_category === "high_speed" && !providerClass) providerClass = "二等座";
  if (providerClass) argv.push("--seat-class-name", providerClass);
  if (request.max_price_yuan !== undefined) argv.push("--max-price", String(request.max_price_yuan));
  if (request.departure_after_hour !== undefined) argv.push("--dep-hour-start", String(request.departure_after_hour));
  // 飞猪的小时上限含这个小时（样本：--arr-hour-end 12 回了 12:35 到的），所以“N 点前”传 N-1
  if (request.departure_before_hour !== undefined) {
    argv.push("--dep-hour-end", String(request.departure_before_hour - 1));
  }
  if (request.arrival_before_hour !== undefined) {
    argv.push("--arr-hour-end", String(request.arrival_before_hour - 1));
  }
  argv.push("--sort-type", SORT_TYPES[request.sort_by]);
  return { command: kind === "flight" ? "search-flight" : "search-train", argv };
}

function localTime(value) {
  const text = safeString(value, 40);
  const match = text?.match(LOCAL_TIME);
  if (!match) return null;
  const [, year, month, day, hour, minute] = match;
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  if (date.toISOString().slice(0, 10) !== `${year}-${month}-${day}` || Number(hour) > 23 || Number(minute) > 59) {
    return null;
  }
  // 飞猪给的是当地时刻、不带时区（样本：上海→东京 01:10 起飞 04:40 到，时长 150 分钟），原样保留，不补时区
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function location(segment, prefix) {
  const city = safeString(segment[`${prefix}CityName`], 80);
  const station = safeString(segment[`${prefix}StationName`], 120);
  const time = localTime(segment[`${prefix}DateTime`]);
  if (!city || !station || !time) return null;
  const result = { city, station };
  const terminal = safeString(segment[`${prefix}Term`], 24);
  if (terminal) result.terminal = terminal;
  result.time = time;
  return { result, code: safeString(segment[`${prefix}StationCode`], 24) };
}

function dayDifference(fromDate, toDate) {
  return Math.round((Date.parse(`${toDate}T00:00:00Z`) - Date.parse(`${fromDate}T00:00:00Z`)) / 86_400_000);
}

function optionId(kind, service) {
  const digest = createHash("sha256")
    .update(
      [kind, service.number, service.departure.station, service.departure.time, service.arrival.station, service.arrival.time].join(
        "\u0000",
      ),
    )
    .digest("hex");
  return `opt_${digest.slice(0, 20)}`;
}

/** 飞猪的一条结果 → 一个直达班次；不是直达、缺关键字段的返回 null。 */
function projectService(item, kind) {
  if (!isRecord(item) || !Array.isArray(item.journeys) || item.journeys.length !== 1) return null;
  const journey = item.journeys[0];
  if (!isRecord(journey) || !Array.isArray(journey.segments) || journey.segments.length !== 1) return null;
  if (![1, "1", "direct", "直达"].includes(journey.journeyType)) return null;
  const segment = journey.segments[0];
  if (!isRecord(segment)) return null;
  const number = safeString(segment.marketingTransportNo, 40);
  const departure = location(segment, "dep");
  const arrival = location(segment, "arr");
  const duration = projectDuration(item.totalDuration);
  if (!number || !departure || !arrival || !duration) return null;
  const dayOffset = dayDifference(departure.result.time.slice(0, 10), arrival.result.time.slice(0, 10));
  if (dayOffset < 0 || dayOffset > 7) return null;

  const service = { number };
  const carrier = safeString(segment.marketingTransportName, 100);
  // 航班是航司简称（东航），火车是车型（高铁、普快）
  if (carrier) service[kind === "flight" ? "airline" : "train_type"] = carrier;
  service.departure = departure.result;
  service.arrival = { ...arrival.result, day_offset: dayOffset };
  service.duration_minutes = duration;
  const travelClass = safeString(segment.seatClassName, 80);
  if (travelClass) service.travel_class = travelClass;
  const price = projectPrice(kind === "flight" ? item.ticketPrice : item.price);
  if (price) service.price_yuan = price.amount_minor / 100;
  const bookingUrl = projectBookingUrl(item.jumpUrl);
  if (bookingUrl) service.booking_url = bookingUrl;
  service.option_id = optionId(kind, service);
  return { service, departureCode: departure.code, arrivalCode: arrival.code };
}

function matchesFilters(service, request, kind) {
  if (request.max_price_yuan !== undefined && (service.price_yuan === undefined || service.price_yuan > request.max_price_yuan)) {
    return false;
  }
  const departureHour = Number(service.departure.time.slice(11, 13));
  if (request.departure_after_hour !== undefined && departureHour < request.departure_after_hour) return false;
  if (request.departure_before_hour !== undefined && departureHour >= request.departure_before_hour) return false;
  if (request.arrival_before_hour !== undefined) {
    // 出发当天、N 点之前到（Fusion 的口径）；飞猪按小时筛但不管日期，跨天到的在这里去掉
    const arrivalHour = Number(service.arrival.time.slice(11, 13));
    if (service.arrival.day_offset !== 0 || arrivalHour >= request.arrival_before_hour) return false;
  }
  if (kind === "train" && request.train_category === "high_speed" && !HIGH_SPEED_NUMBER.test(service.number)) {
    return false;
  }
  return true;
}

function sortServices(services, sortBy) {
  if (sortBy === "recommended") return services;
  const key = {
    price_asc: (s) => s.price_yuan ?? Number.POSITIVE_INFINITY,
    duration_asc: (s) => s.duration_minutes,
    departure_asc: (s) => Date.parse(`${s.departure.time}:00Z`),
  }[sortBy];
  return services
    .map((service, index) => ({ service, index }))
    .sort((a, b) => key(a.service) - key(b.service) || a.index - b.index)
    .map(({ service }) => service);
}

function located(entry, which, requested) {
  const place = entry.service[which];
  return placeMatches(requested, {
    city: place.city,
    station_name: place.station,
    station_code: which === "departure" ? entry.departureCode : entry.arrivalCode,
  });
}

/**
 * 飞猪返回 → 工具结果。失败抛 ToolFailure。
 * 飞猪认不出的地名不报错，会换成别的城市（样本：起点“火星”回了长沙→上海），所以按起终点核对，全对不上就报认不出。
 */
export function shapeToolResult(payload, request, kind, observedAt) {
  let items;
  try {
    items = extractItemList(payload);
  } catch (error) {
    // status != 0：没有直达班次和查询出错都是这样（样本：“搜索火车票行业失败”“搜索机票低价行业失败”），分不出来
    if (error?.code === "provider_error") throw new ToolFailure("provider_no_result");
    throw new ToolFailure("unavailable");
  }
  if (items.length === 0) throw new ToolFailure("no_services");

  const entries = items.slice(0, 100).map((item) => projectService(item, kind)).filter(Boolean);
  if (entries.length === 0) throw new ToolFailure("unavailable");
  const fromOrigin = entries.filter((entry) => located(entry, "departure", request.origin));
  if (fromOrigin.length === 0) {
    throw new ToolFailure("origin_not_recognized", "", { returned_city: entries[0].service.departure.city });
  }
  const routed = fromOrigin.filter((entry) => located(entry, "arrival", request.destination));
  if (routed.length === 0) {
    throw new ToolFailure("destination_not_recognized", "", { returned_city: fromOrigin[0].service.arrival.city });
  }

  const seen = new Set();
  const onDate = [];
  for (const { service } of routed) {
    if (service.departure.time.slice(0, 10) !== request.departure_date || seen.has(service.option_id)) continue;
    seen.add(service.option_id);
    onDate.push(service);
  }
  const matching = onDate.filter((service) => matchesFilters(service, request, kind));
  const services = sortServices(matching, request.sort_by).slice(0, request.limit);
  // 飞猪那一页里有这条线路、这天的班次，只是都不满足条件：也算没查到，但要让模型知道是条件筛掉的
  if (services.length === 0) throw new ToolFailure("no_services", "", { filtered: onDate.length > 0 });

  const limitations = [];
  const fullPage = items.length >= PROVIDER_PAGE_SIZE;
  if (fullPage && matching.length < onDate.length && services.length < request.limit) {
    limitations.push("filtered_partial_list");
  }
  const spec = classSpec(request, kind);
  if (spec && services.some((service) => service.travel_class && !spec.aliases.includes(service.travel_class))) {
    limitations.push("other_class_returned");
  }
  if (
    kind === "train" &&
    request.train_category === "all" &&
    fullPage &&
    services.length > 0 &&
    !onDate.some((service) => HIGH_SPEED_NUMBER.test(service.number))
  ) {
    limitations.push("high_speed_not_covered");
  }
  return { ok: true, observed_at: observedAt, query: request, services, limitations };
}
