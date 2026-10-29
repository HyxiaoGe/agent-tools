# context7

Context7 官方 MCP 的代理，说明见 [README](README.md)。

- 密钥只在这里以 `CONTEXT7_API_KEY` 请求头发给上游；不进日志（httpx 日志已压到 WARNING）、不进报错、不进工具结果。
- 只放行 `DOCUMENTATION_TOOLS` 里的文档工具，列表过滤和调用拒绝都在 `Context7Contract` 里；改白名单要同步 README 与 Fusion 的 `provider_profiles`。
- 上游结果原样透传，只在被限流时附加结构化错误；参数约束、Library ID 来源校验留在产品侧。
- 不转发调用方任何入站头给 Context7。
