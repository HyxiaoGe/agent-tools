// 状态目录里的两样东西：
// - device-id：飞猪 CLI 的设备标识，固定一个（见 flyai-runner.js 的说明）。
// - usage.json：每天（北京时间）实际启动了几次 CLI、失败几次、按错误代号分，给网关日报看（GET /usage/daily）。
//   只留最近 3 天，和别的服务一样。
// 状态目录不可写时（本地跑、只读容器没挂卷）：设备标识用进程内的一个，计数只在内存里，重启清零。

import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import { chinaToday } from "./travel.js";

export const DEVICE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
const KEEP_DAYS = 3;
const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/u;
const CODE_PATTERN = /^[a-z_]{1,40}$/u;

/** 读状态目录里的设备标识，没有就生成一个存下；目录不可用时返回进程内的一个，第二个值是是否存下了。 */
export async function loadDeviceId(stateDir) {
  if (!stateDir) return { deviceId: randomUUID(), persisted: false };
  const file = path.join(stateDir, "device-id");
  try {
    await mkdir(stateDir, { recursive: true, mode: 0o700 });
    try {
      const saved = (await readFile(file, "utf8")).trim();
      if (DEVICE_ID_PATTERN.test(saved)) return { deviceId: saved, persisted: true };
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    const deviceId = randomUUID();
    await writeFile(file, `${deviceId}\n`, { mode: 0o600 });
    return { deviceId, persisted: true };
  } catch {
    return { deviceId: randomUUID(), persisted: false };
  }
}

export class UsageCounter {
  constructor({ stateDir = null, clock = () => new Date() } = {}) {
    this.file = stateDir ? path.join(stateDir, "usage.json") : null;
    this.clock = clock;
    this.days = {};
    this.persistent = false;
    this.writing = Promise.resolve();
  }

  async load() {
    if (!this.file) return;
    try {
      await mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
      try {
        const saved = JSON.parse(await readFile(this.file, "utf8"));
        this.days = sanitize(saved);
      } catch (error) {
        // 文件坏了就从空的开始，不影响查询
        if (error?.code !== "ENOENT" && !(error instanceof SyntaxError)) throw error;
      }
      this.persistent = true;
    } catch {
      this.persistent = false;
    }
  }

  /** 记一次 CLI 调用：code 为 null 是成功，否则是失败的代号。 */
  record(code) {
    const day = chinaToday(this.clock());
    const entry = (this.days[day] ??= { calls: 0, failed: 0, failures: {} });
    entry.calls += 1;
    if (code) {
      entry.failed += 1;
      const key = CODE_PATTERN.test(code) ? code : "internal_error";
      entry.failures[key] = (entry.failures[key] ?? 0) + 1;
    }
    for (const saved of Object.keys(this.days)) {
      if (dayDiff(saved, day) >= KEEP_DAYS) delete this.days[saved];
    }
    if (this.persistent) this.writing = this.writing.then(() => this.flush()).catch(() => {});
  }

  async flush() {
    const temporary = `${this.file}.tmp`;
    await writeFile(temporary, JSON.stringify(this.days), { mode: 0o600 });
    await rename(temporary, this.file);
  }

  summary(day) {
    const entry = this.days[day] ?? { calls: 0, failed: 0, failures: {} };
    return { day, available: true, persistent: this.persistent, ...structuredClone(entry) };
  }
}

function dayDiff(from, to) {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}

function sanitize(saved) {
  const days = {};
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return days;
  for (const [day, entry] of Object.entries(saved)) {
    if (!DAY_PATTERN.test(day) || !entry || typeof entry !== "object") continue;
    const failures = {};
    for (const [code, count] of Object.entries(entry.failures ?? {})) {
      if (CODE_PATTERN.test(code) && Number.isSafeInteger(count) && count > 0) failures[code] = count;
    }
    const calls = Number.isSafeInteger(entry.calls) && entry.calls >= 0 ? entry.calls : 0;
    const failed = Number.isSafeInteger(entry.failed) && entry.failed >= 0 ? entry.failed : 0;
    days[day] = { calls, failed, failures };
  }
  return days;
}

export function isDay(value) {
  if (typeof value !== "string" || !DAY_PATTERN.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
