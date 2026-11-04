import process from "node:process";

import { createAdapterServer } from "./server.js";
import { UsageCounter, loadDeviceId } from "./usage.js";

function requiredEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} 必须配置`);
  return value;
}

try {
  // 设备标识和每日计数存在这里（compose 挂的卷），重启不丢
  const stateDir = process.env.FLYAI_STATE_DIR?.trim() || "/var/lib/flyai";
  const { deviceId, persisted } = await loadDeviceId(stateDir);
  const usage = new UsageCounter({ stateDir });
  await usage.load();
  if (!persisted || !usage.persistent) {
    process.stderr.write("flyai-adapter 状态目录不可写：设备标识和计数只在内存里，重启后重新生成\n");
  }
  const server = createAdapterServer({
    apiKey: requiredEnvironment("FLYAI_API_KEY"),
    homeDir: "/tmp/flyai-home",
    deviceId,
    usage,
  });
  const port = Number(process.env.PORT ?? "8093");
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("PORT 无效");
  const host = process.env.HOST?.trim() || "0.0.0.0";
  if (!new Set(["0.0.0.0", "127.0.0.1", "::1"]).has(host)) throw new Error("HOST 无效");
  server.listen(port, host, () => {
    process.stdout.write(`flyai-adapter 已启动 host=${host} port=${port}\n`);
  });
} catch {
  process.stderr.write("flyai-adapter 启动失败\n");
  process.exitCode = 1;
}
