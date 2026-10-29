# tmap

腾讯地图（腾讯位置服务）官方 MCP 的代理，说明见 [README](README.md)。

- 密钥只在这里注入上游地址；不进日志（httpx 日志已压到 WARNING）、不进报错、不进工具结果。
- 只放行 `READ_ONLY_TOOLS` 里的只读工具，列表过滤和调用拒绝都在 `TmapContract` 里；改白名单要同步 README 与 Fusion 管理页里该服务的授权工具。
- 上游结果原样透传；不在这里做产品编排或改写输出格式。
- 不转发调用方任何入站头给腾讯。
