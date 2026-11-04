// MCP 工具 search_flights / search_trains 的测试。飞猪返回用 test/fixtures/ 里 11-04 录的真实样本
// （预订链接换成了假的路径），CLI 用假的 runner 代替，不调真实服务。

import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { runFlyaiCli } from "../src/flyai-runner.js";
import { ConcurrencyGate } from "../src/gate.js";
import { createAdapterServer } from "../src/server.js";
import { UsageCounter, loadDeviceId } from "../src/usage.js";

const API_KEY = "test-flyai-key";
const FIXTURES = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures");
// 北京时间 2026-11-04 10:00，样本查的是 11-12
const NOW = new Date("2026-11-04T02:00:00Z");
const HEADERS = { "x-caller-app": "lingdraft", "x-caller-user": "user-1" };

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURES, `${name}.json`), "utf8"));
}

function fakeRunner(pick) {
  const calls = [];
  const runner = async ({ command, argv, signal }) => {
    calls.push({ command, argv: [...argv] });
    return pick({ command, argv, signal });
  };
  return { runner, calls };
}

async function startServer(options) {
  const usage = new UsageCounter({ clock: () => NOW });
  const server = createAdapterServer({ apiKey: API_KEY, clock: () => NOW, usage, log: () => {}, ...options });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const { port } = server.address();
  return { server, usage, url: `http://127.0.0.1:${port}` };
}

async function stopServer(server) {
  server.closeAllConnections();
  server.close();
  await once(server, "close");
}

async function connect(url, headers = HEADERS) {
  const client = new Client({ name: "test", version: "1.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(`${url}/mcp`), { requestInit: { headers } }));
  return client;
}

async function withServer(options, body) {
  const started = await startServer(options);
  const client = await connect(started.url);
  try {
    await body({ ...started, client });
  } finally {
    await client.close();
    await stopServer(started.server);
  }
}

function argValue(argv, flag) {
  const index = argv.indexOf(flag);
  return index === -1 ? undefined : argv[index + 1];
}

function rpc(method, params, id = 1) {
  return JSON.stringify({ jsonrpc: "2.0", id, method, params });
}

async function rawPost(url, body) {
  return fetch(`${url}/mcp`, {
    method: "POST",
    headers: { ...HEADERS, "content-type": "application/json", accept: "application/json, text/event-stream" },
    body,
  });
}

const TRAIN_ARGS = { origin: "北京", destination: "哈尔滨", departure_date: "2026-11-12" };

test("列出两个工具：说明是英文、只读，火车必须写明要不要只看高铁", async () => {
  await withServer({ runner: async () => assert.fail("不该调用飞猪") }, async ({ client }) => {
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map((tool) => tool.name).sort(), ["search_flights", "search_trains"]);
    for (const tool of tools) {
      assert.equal(tool.annotations.readOnlyHint, true);
      // 中文只出现在地名、车型的例子里（上海、浦东机场、高铁），没有中文句子
      assert.doesNotMatch(tool.description, /[一-鿿]{6,}/u);
      assert.match(tool.description, /ask which date first/u);
    }
    const train = tools.find((tool) => tool.name === "search_trains");
    assert.ok(train.inputSchema.required.includes("train_category"));
    assert.ok(!train.inputSchema.required.includes("seat_class"));
  });
});

test("握手和列工具都带 x-upstream-calls: 0，网关不计入每日上限", async () => {
  const started = await startServer({ runner: async () => assert.fail("不该调用飞猪") });
  try {
    const response = await rawPost(started.url, rpc("tools/list", {}));
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-upstream-calls"), "0");
    const get = await fetch(`${started.url}/mcp`, { headers: HEADERS });
    assert.equal(get.status, 405);
    assert.equal(get.headers.get("x-upstream-calls"), "0");
    const noApp = await fetch(`${started.url}/mcp`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
      body: rpc("tools/list", {}),
    });
    assert.equal(noApp.status, 400);
  } finally {
    await stopServer(started.server);
  }
});

test("只要高铁：带二等座去查，只留 G/D/C；模型那份没有预订链接", async () => {
  const payload = await fixture("train_bj_hrb_2nd");
  const { runner, calls } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_trains",
      arguments: { ...TRAIN_ARGS, train_category: "high_speed" },
    });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].command, "search-train");
    assert.equal(argValue(calls[0].argv, "--seat-class-name"), "二等座");
    assert.equal(argValue(calls[0].argv, "--journey-type"), "1");
    assert.equal(argValue(calls[0].argv, "--sort-type"), "2");

    const data = result.structuredContent;
    assert.equal(data.ok, true);
    assert.equal(data.observed_at, "2026-11-04T10:00:00+08:00");
    assert.equal(data.services.length, 5);
    for (const service of data.services) {
      assert.match(service.number, /^[GDC]\d/u);
      assert.match(service.option_id, /^opt_[0-9a-f]{20}$/u);
      assert.match(service.departure.time, /^2026-11-12T\d{2}:\d{2}$/u);
      assert.equal(service.travel_class, "二等座");
      assert.equal(typeof service.price_yuan, "number");
      assert.match(service.booking_url, /^https:\/\/router\.feizhu\.com\//u);
    }
    assert.deepEqual(data.limitations, []);
    const text = JSON.parse(result.content[0].text);
    assert.equal(text.ok, undefined);
    assert.equal(text.services[0].booking_url, undefined);
    assert.equal(text.services[0].number, data.services[0].number);
  });
});

test("全部车型、飞猪那一页没有高铁：标出 high_speed_not_covered", async () => {
  const payload = await fixture("train_bj_hrb");
  const { runner, calls } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({ name: "search_trains", arguments: { ...TRAIN_ARGS, train_category: "all" } });
    assert.equal(argValue(calls[0].argv, "--seat-class-name"), undefined);
    const data = result.structuredContent;
    assert.ok(data.services.every((service) => !/^[GDC]\d/u.test(service.number)));
    assert.deepEqual(data.limitations, ["high_speed_not_covered"]);
  });
});

test("先筛后截：N 点前到传 N-1 给飞猪，跨天到的在这里去掉；筛完一个不剩算 no_services", async () => {
  const payload = await fixture("train_gz_lasa");
  const { runner, calls } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const all = await client.callTool({
      name: "search_trains",
      arguments: { origin: "广州", destination: "拉萨", departure_date: "2026-11-12", train_category: "all" },
    });
    const service = all.structuredContent.services[0];
    assert.ok(service.duration_minutes > 2_880, "两天以上的车也要留下");
    assert.ok(service.arrival.day_offset >= 2);

    const limited = await client.callTool({
      name: "search_trains",
      arguments: {
        origin: "广州",
        destination: "拉萨",
        departure_date: "2026-11-12",
        train_category: "all",
        arrival_before_hour: 23,
      },
    });
    assert.equal(argValue(calls[1].argv, "--arr-hour-end"), "22");
    assert.equal(limited.structuredContent.ok, false);
    assert.equal(limited.structuredContent.error.code, "no_services");
    assert.match(limited.content[0].text, /none matched the requested time, price or class limits/u);
    assert.equal(limited.isError, undefined);
  });
});

test("要头等舱飞猪回公务舱：不丢班次，标出 other_class_returned", async () => {
  const payload = await fixture("flight_bj_sh_first");
  const { runner, calls } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_flights",
      arguments: { origin: "北京", destination: "上海", departure_date: "2026-11-12", cabin_class: "first", sort_by: "price_asc" },
    });
    assert.equal(calls[0].command, "search-flight");
    assert.equal(argValue(calls[0].argv, "--seat-class-name"), "头等舱");
    assert.equal(argValue(calls[0].argv, "--sort-type"), "3");
    const data = result.structuredContent;
    assert.equal(data.services[0].travel_class, "公务舱");
    assert.ok(data.services[0].airline);
    const prices = data.services.map((service) => service.price_yuan);
    assert.deepEqual(prices, [...prices].sort((a, b) => a - b));
    assert.deepEqual(data.limitations, ["other_class_returned"]);
  });
});

test("机场简称也能对上：虹桥机场、大兴机场", async () => {
  const payload = await fixture("flight_bj_sh_first");
  const { runner } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_flights",
      arguments: { origin: "大兴机场", destination: "虹桥机场", departure_date: "2026-11-12" },
    });
    const data = result.structuredContent;
    assert.equal(data.ok, true);
    for (const service of data.services) {
      assert.equal(service.departure.station, "大兴国际机场");
      assert.equal(service.arrival.station, "虹桥国际机场");
    }
  });
});

test("国际航班：时刻原样是当地时间，不补时区", async () => {
  const payload = await fixture("flight_sh_tokyo");
  const { runner } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_flights",
      arguments: { origin: "上海", destination: "东京", departure_date: "2026-11-12" },
    });
    const raw = payload.data.itemList[0].journeys[0].segments[0];
    const service = result.structuredContent.services.find((item) => item.number === raw.marketingTransportNo);
    assert.equal(service.arrival.time, raw.arrDateTime.slice(0, 16).replace(" ", "T"));
    assert.doesNotMatch(service.arrival.time, /[+Z]/u);
  });
});

test("飞猪认不出的起点会换成别的城市：按起点核对，报 origin_not_recognized", async () => {
  const payload = await fixture("train_unknown");
  const { runner } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_trains",
      arguments: { origin: "火星", destination: "上海", departure_date: "2026-11-12", train_category: "all" },
    });
    assert.deepEqual(result.structuredContent, {
      ok: false,
      error: { code: "origin_not_recognized", message: "The travel service did not recognize the origin.", retryable: false },
    });
    assert.match(result.content[0].text, /did not recognize the origin "火星"/u);
    assert.doesNotMatch(result.content[0].text, /长沙/u);
  });
});

test("县级市的车站挂在地级市下：义乌站（金华）也算义乌", async () => {
  const payload = await fixture("train_yiwu_bj");
  const { runner } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_trains",
      arguments: { origin: "义乌", destination: "北京", departure_date: "2026-11-12", train_category: "all" },
    });
    assert.equal(result.structuredContent.ok, true);
    assert.equal(result.structuredContent.services[0].departure.city, "金华");
  });
});

test("飞猪 status 非 0：没有直达和查询出错分不开，报 provider_no_result，不让模型重查", async () => {
  const payload = await fixture("train_hs_kashi");
  const { runner } = fakeRunner(() => payload);
  await withServer({ runner }, async ({ client }) => {
    const result = await client.callTool({
      name: "search_trains",
      arguments: { origin: "黄山", destination: "喀什", departure_date: "2026-11-12", train_category: "all" },
    });
    assert.equal(result.structuredContent.error.code, "provider_no_result");
    assert.match(result.content[0].text, /Do not repeat the same query/u);
  });
});

test("过去的日期、起终点相同：不调飞猪，响应带 x-upstream-calls: 0", async () => {
  const { runner, calls } = fakeRunner(() => assert.fail("不该调用飞猪"));
  const started = await startServer({ runner });
  try {
    for (const args of [
      { ...TRAIN_ARGS, departure_date: "2026-11-03", train_category: "all" },
      { ...TRAIN_ARGS, destination: "北京", train_category: "all" },
      { ...TRAIN_ARGS, departure_date: "2026-02-30", train_category: "all" },
      { ...TRAIN_ARGS, train_category: "all", departure_after_hour: 12, departure_before_hour: 9 },
    ]) {
      const response = await rawPost(started.url, rpc("tools/call", { name: "search_trains", arguments: args }));
      assert.equal(response.headers.get("x-upstream-calls"), "0");
      const body = await response.json();
      assert.equal(body.result.structuredContent.error.code, "invalid_arguments");
    }
    // 没过 schema 的由 SDK 回 isError
    const response = await rawPost(started.url, rpc("tools/call", { name: "search_trains", arguments: TRAIN_ARGS }));
    assert.equal(response.headers.get("x-upstream-calls"), "0");
    assert.equal((await response.json()).result.isError, true);
    assert.equal(calls.length, 0);
  } finally {
    await stopServer(started.server);
  }
});

test("调了飞猪的响应不带 x-upstream-calls；可选参数传 null 当作没传", async () => {
  const payload = await fixture("train_bj_hrb");
  const { runner, calls } = fakeRunner(() => payload);
  const started = await startServer({ runner });
  try {
    const response = await rawPost(
      started.url,
      rpc("tools/call", {
        name: "search_trains",
        arguments: { ...TRAIN_ARGS, train_category: "all", seat_class: null, sort_by: null, max_price_yuan: null },
      }),
    );
    assert.equal(response.headers.get("x-upstream-calls"), null);
    assert.equal((await response.json()).result.structuredContent.ok, true);
    assert.equal(argValue(calls[0].argv, "--max-price"), undefined);
  } finally {
    await stopServer(started.server);
  }
});

test("同一用户上一次还没完：排队等，等不到报 busy（可重试），不调飞猪", async () => {
  let release;
  const blocked = new Promise((resolve) => {
    release = resolve;
  });
  const payload = await fixture("train_bj_hrb");
  const { runner, calls } = fakeRunner(async () => {
    await blocked;
    return payload;
  });
  await withServer({ runner, mcpQueueTimeoutMs: 50 }, async ({ client }) => {
    const first = client.callTool({ name: "search_trains", arguments: { ...TRAIN_ARGS, train_category: "all" } });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const second = await client.callTool({ name: "search_trains", arguments: { ...TRAIN_ARGS, train_category: "all" } });
    assert.equal(second.structuredContent.error.code, "busy");
    assert.equal(second.structuredContent.error.retryable, true);
    release();
    assert.equal((await first).structuredContent.ok, true);
    assert.equal(calls.length, 1);
  });
});

test("CLI 超时报 timeout，模型那份只说没查到；计数记下失败", async () => {
  const { runner } = fakeRunner(() => {
    throw Object.assign(new Error("provider_timeout"), { code: "provider_timeout" });
  });
  await withServer({ runner }, async ({ client, url }) => {
    const result = await client.callTool({ name: "search_trains", arguments: { ...TRAIN_ARGS, train_category: "all" } });
    assert.equal(result.structuredContent.error.code, "timeout");
    assert.match(result.content[0].text, /could not be completed/u);
    const usage = await (await fetch(`${url}/usage/daily?day=2026-11-04`)).json();
    assert.deepEqual(usage, {
      day: "2026-11-04",
      available: true,
      persistent: false,
      calls: 1,
      failed: 1,
      failures: { provider_timeout: 1 },
    });
    assert.equal((await fetch(`${url}/usage/daily?day=2026-13-01`)).status, 400);
  });
});

test("并发闸门：排队按顺序放行，同一用户串行，取消的人离队", async () => {
  const gate = new ConcurrencyGate(2);
  assert.equal(gate.tryAcquire("a"), true);
  assert.equal(gate.tryAcquire("a"), false);
  assert.equal(gate.tryAcquire("c"), true);
  // 全局已满（a、c）：a 的第二次和 b 都排队；取消 b
  const waitingA = gate.acquire("a", { timeoutMs: 1_000 });
  const controller = new AbortController();
  const cancelled = gate.acquire("b", { timeoutMs: 1_000, signal: controller.signal });
  assert.equal(gate.waiters.length, 2);
  controller.abort();
  assert.equal(await cancelled, false);
  gate.release("a");
  assert.equal(await waitingA, true);
  assert.equal(gate.activeGlobal, 2);
  const timedOut = await gate.acquire("d", { timeoutMs: 10 });
  assert.equal(timedOut, false);
  gate.release("a");
  gate.release("c");
  assert.equal(gate.activeGlobal, 0);
  assert.equal(gate.waiters.length, 0);
});

test("计数和设备标识存在状态目录，重启读回；只留 3 天", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "flyai-state-test-"));
  try {
    const first = await loadDeviceId(stateDir);
    assert.equal(first.persisted, true);
    assert.equal((await loadDeviceId(stateDir)).deviceId, first.deviceId);

    let now = new Date("2026-11-01T02:00:00Z");
    const usage = new UsageCounter({ stateDir, clock: () => now });
    await usage.load();
    usage.record(null);
    now = new Date("2026-11-04T02:00:00Z");
    usage.record(null);
    usage.record("provider_error");
    await usage.writing;

    const reloaded = new UsageCounter({ stateDir, clock: () => now });
    await reloaded.load();
    assert.equal(reloaded.summary("2026-11-01").calls, 0, "3 天前的已删");
    assert.deepEqual(reloaded.summary("2026-11-04"), {
      day: "2026-11-04",
      available: true,
      persistent: true,
      calls: 2,
      failed: 1,
      failures: { provider_error: 1 },
    });

    await writeFile(path.join(stateDir, "usage.json"), "{broken");
    const broken = new UsageCounter({ stateDir, clock: () => now });
    await broken.load();
    assert.equal(broken.persistent, true);
    assert.equal(broken.summary("2026-11-04").calls, 0);
  } finally {
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("CLI runner 把固定的设备标识放进每次调用的临时 HOME", async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "flyai-device-test-"));
  const fixturePath = path.join(tempDir, "fixture.mjs");
  await writeFile(
    fixturePath,
    `
      import { readFile } from "node:fs/promises";
      import path from "node:path";
      const id = await readFile(path.join(process.env.HOME, ".flyai", "device-id"), "utf8");
      process.stdout.write(JSON.stringify({ status: 0, message: id.trim(), data: { itemList: [] } }));
    `,
  );
  const deviceId = "0f8fad5b-d9cb-469f-a165-70867728950e";
  try {
    const result = await runFlyaiCli({
      command: "search-train",
      argv: ["--origin", "北京"],
      apiKey: API_KEY,
      homeDir: tempDir,
      executablePath: process.execPath,
      bundlePath: fixturePath,
      deviceId,
    });
    assert.equal(result.message, deviceId);
    await assert.rejects(
      runFlyaiCli({
        command: "search-train",
        argv: [],
        apiKey: API_KEY,
        homeDir: tempDir,
        executablePath: process.execPath,
        bundlePath: fixturePath,
        deviceId: "../../etc",
      }),
      { code: "invalid_invocation" },
    );
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
});
