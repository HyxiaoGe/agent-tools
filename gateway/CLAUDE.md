# gateway

agent-tools 的统一入口，说明见 [README](README.md)。

- 只做鉴权、记账、转发，不放业务逻辑；服务自己的规则留在各服务里。
- 每日日报只读用量记录、向 search / reader / maps / flyai 拉 `/usage/daily`、向 image 拉 `/v1/quota`，不改变转发行为；服务内部的上游明细由服务自己提供。
- 产品能调哪些服务由 `PRODUCTS[*].services` 决定，新接入的付费服务不进默认列表；网关只按服务放行，工具级白名单放在各服务的契约层。
- 产品身份只来自服务密钥；调用方声明的用户 / 场景只用于统计，转发前统一重写身份头，不透传调用方自带的身份头。
- 每个上游独立连接池与超时；网关自己判定的失败（busy / timeout / unreachable / 每日上限 limited）明确返回并记账，不重试、不换上游。
- 每日上限只算 REST 请求和 MCP `tools/call`（`proxy.counts_as_call`，日报同口径），不拦 MCP 握手；它是最外层兜底，按上游的额度留在各服务里。服务总上限和产品上限（`PRODUCTS[*].daily_limits`）同时生效。
- 没调上游的请求只认服务的声明（响应头 `X-Upstream-Calls: 0`）和网关自己的 busy，不按状态码推断（`proxy.consumes_quota`）。
- 密钥不进日志、报错和用量记录。
