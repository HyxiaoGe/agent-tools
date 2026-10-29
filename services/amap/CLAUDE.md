# amap

高德官方 MCP 的代理，说明见 [README](README.md)。

- 密钥只在这里注入上游地址；不进日志（httpx 日志已压到 WARNING）、不进报错、不进工具结果。
- 只放行 `READ_ONLY_TOOLS` 里的只读工具，列表过滤和调用拒绝都在 `AmapContract` 里；改白名单要同步 README 与 Fusion 的 `provider_profiles`。
- 上游结果原样透传，只在额度用尽时附加结构化错误；不在这里做产品编排（地点搜索、路线对比留在产品侧）。
- 不转发调用方任何入站头给高德。
