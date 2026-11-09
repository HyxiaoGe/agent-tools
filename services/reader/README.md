# reader-service

输入一个 URL，返回 Markdown 正文；读不到时返回结构化的失败原因，调用方据此告诉模型该来源不可用。

两种接法，读取走同一段（`main.read_page`）：

- REST `GET /read`：线上 Fusion 的 `url_read` 在用，返回上游原文，包装头剥离、截断、网址检查由调用方自己做；保持不变。
- MCP 工具 `read_page`（`/mcp`，经网关 `/reader/mcp`）：给产品的模型直接用，网址检查、正文整理、失败分类和英文说明都在服务里，见 [MCP](#mcp-read_page)。

## 接口

### `GET /read?url=<URL>[&full=true]`

- `full=true`：跳过正文提取，返回整页 Markdown（表格、列表类页面用）。

成功返回：

```json
{
  "url": "...",
  "title": "...",
  "content": "Markdown 正文",
  "favicon": "...",
  "content_length": 1234,
  "fetch_ms": 1800,
  "attempts": 2,
  "provider": "direct | firecrawl | tavily | jina"
}
```

失败时 HTTP 状态码非 200，`detail` 为：

```json
{"kind": "...", "message": "...", "retryable": false, "upstream_status": 404, "attempts": 2, "duration_ms": 3100}
```

| HTTP | kind | 含义 |
|---|---|---|
| 404 | `page_not_found` | 原网页返回 404/410，网页确定不存在，不要重试；`upstream_status` 为原网页状态码 |
| 502 | `page_error` | 原网页返回其他错误状态码（如 403），错误页不当正文 |
| 504 | `timeout` | 总时限内没读到 |
| 503 | `rate_limited` | Jina 限流 |
| 429 | `daily_quota_exceeded` | Jina 今日额度已用完（见[每日上游额度](#每日上游额度)），不要重试，北京时间 0 点重置 |
| 502 | `upstream_auth` | Jina 号池全部 key 不可用 |
| 502 | `upstream_error` / `request_error` | Jina 返回其他错误或请求失败 |
| 422 | — | URL 格式无效 |

### `GET /usage/daily[?day=YYYY-MM-DD]`

某天（北京时间，默认今天）各上游的调用 / 被每日额度拦下次数，供网关日报读取；只有计数，计数保留 3 天，更早的日期全是 0。

```json
{"day": "2026-11-01", "available": true, "providers": [{"provider": "firecrawl", "limit": 300, "used": 42, "rejected": 0}, …]}
```

### `GET /health`

只看各号池状态，不调用上游、不消耗额度。某个号池的 key 全部不可用（密钥无效、额度用完或限流）时返回 503，正文里按 `jina_account` / `tavily_account` / `firecrawl_account` 给出序号和状态码（不含 key），dev-ops-sentinel 据此告警；冷却到期自动恢复。

## MCP `read_page`

无状态的 streamable HTTP（`/mcp`，不用先 initialize，每个请求一个 JSON-RPC），工具只有 `read_page(url, full_page=false)`。说明是英文，写在 `mcp_server.py`，只讲工具本身；什么时候读、读哪些由产品在自己的提示词里接。

读之前查网址（`url_safety.py`），不合法、不该读的不交给任何上游：

- `invalid_url`：空、超过 4096 字符、不是 http/https、没有主机名、端口不对；
- `unsafe_url`：带账号密码；私网、本机、保留地址（含 `2130706433`、`0x7f.0.0.1`、`127.1` 这些写法和映射到 IPv4 的 IPv6）；`localhost`、`.local` `.localhost` `.internal` `.lan` `.home` 结尾或没有点的主机名；参数里有凭据类键名且值不为空（`token`、`api_key`、`signature`、`password`、`session` 等，按完整键名认，`keyword`、`author`、`code` 不算）；域名经 DoH 解析出私网地址（解析失败不拦，直连自己会再查）。
- 锚点去掉后再读，不交给上游，所以锚点里的内容不查。

读到的原文整理成给模型的正文（`page_text.py`，规则照 Fusion）：剥掉 Jina 的包装头；去掉包装和图片后没有字的算失败（包装头 Warning 写了验证码、拒绝访问、要登录的是 `access_blocked`，其余 `empty_content`，不按正文里的词判）；正文里有和标题一字不差的一级标题时从它开始（跳过页首导航，代码块里的不算）；截断一次，8000 字。

成功：`structuredContent` 是

```json
{"ok": true, "url": "...", "title": "...", "content": "...", "content_chars": 12034, "truncated": true,
 "extracted": true, "note": "...", "meta": {"provider": "direct", "attempts": 1, "fetch_ms": 900, "favicon": "https://..."}}
```

`truncated`、`extracted`（直接抓取、经正文提取的，表格列表可能缺，可用 `full_page=true` 再读）只在为真时出现，`note` 把它们写成给模型的一句话。`meta.favicon` 是站点图标（给产品画来源卡，只认 http(s)、不超过 500 字，没有就不给）。给模型的文字是去掉 `meta` 的同一份 JSON。

失败：`{"ok": false, "error": {"code", "message", "retryable", "status"?}}`，`isError` 为 false，文字里写清下一步怎么做、不要编造页面内容。

| code | 含义 | retryable |
|---|---|---|
| `invalid_url` / `unsafe_url` | 见上，没有读 | 否 |
| `page_not_found` | 原网页 404/410 | 否 |
| `access_blocked` | 原网页 401/403，或验证码、登录墙 | 否 |
| `page_error` | 原网页其他错误状态码，`status` 是状态码 | 408/425/429/5xx 是 |
| `empty_content` | 读回来了但没有正文 | 否 |
| `timeout` | 10s 内没读到 | 是 |
| `rate_limited` | Jina 限流 | 是 |
| `quota_exhausted` | Jina 今日额度用完 | 否 |
| `upstream_error` | Jina 号池不可用、出错或请求失败 | 看上游 |

全部失败时 REST 只报 Jina 的原因；Firecrawl 看到原网页报错而 Jina 只是超时或出错时，MCP 报原网页的错误（更能说明问题）。

## 读取调度

一次读取的总时限 10s（`READ_TOTAL_TIMEOUT`），超时即放弃该网页：Fusion 会并行读多个网页、等最慢的那个，读不到的要尽快放弃。

1. **0s：直接抓取**（`direct_read.py`）。DoH 解析并核验全部是公网地址后直连，trafilatura 提取正文。免费，成功即返回。
   代码块和标签页提取前先处理（`code_blocks.py`）：trafilatura 按 class 去噪，文档站包代码的 div 常带 scrollbar、button 这类词，整块代码被扔掉（docs.langchain.com 一页 27 段一段不剩），列表项里的代码被压成一行。所以每段代码先换成占位、提取完换回 Markdown 代码块（带页面标的语言、文件名）；标签页没选中的那几页也显示，页首加标签名。代码在侧栏、页脚这类被去掉的地方时照样不要。
2. **1.5s 还没成功**（`DIRECT_HEDGE_DELAY`，直接抓取先失败则立即）：**同时请求 Firecrawl 和 Tavily**，直接抓取继续跑，谁先读到正文用谁。
3. **Firecrawl 报原网页 404/410**：立即结束，返回 `page_not_found`，不再等其他方式。
4. **Jina 只兜底**：Firecrawl 失败（超时、被拦截、原网页 403 等其他错误码、无正文、号池全部不可用）后才启动；没配 Firecrawl 时第 2 步直接带上 Jina。直接抓取打开了页面（2xx 的 HTML）、只是提不出正文时（多半是脚本渲染的页面，如今日头条文章）也直接带上：页面不是错误页，不必等 Firecrawl，它对这类页面常几秒后才报错，再轮到 Jina 就过了总时限。Jina 会把错误页包装成 200 返回，这里按它头部的 `Warning: Target URL returned error <状态码>` 识别，错误页不当正文。

为什么 Firecrawl 为主：同一批 120 个网址实测，Firecrawl 中位 4.3s、Jina 8.6s，两者读到真正正文的页数相当；Firecrawl 会报告原网页状态码，Jina 读到的页里约一成是 404 等错误页。

## 每日上游额度

兜底防止批量实验或失控循环把付费上游打穿。`PROVIDER_DAILY_LIMITS`（`上游=次数`，逗号分隔，北京时间自然日），默认 `firecrawl=300,tavily=300,jina=300`（10 月以来 reader 单日峰值 127 次读取；Firecrawl 和 search 用的是同一个账号）。网关另有 `reader=1000` 的每日总上限。

- 一次读取真正请求某家上游时算一次；同一读取内换 key、Jina 重试不另算；号池没有可用 key、没发出请求时不算。直接抓取免费，不计。未列出的上游不限但照样计数，`0` 表示当天不调；写了未知上游名，服务起不来。
- 用满后当天跳过该上游：Firecrawl 用满时直接请求 Jina，Tavily 用满时不再请求；Jina 用满时它的兜底返回 429 `daily_quota_exceeded`（不可重试）。三家都用满时只剩直接抓取。
- 服务是单进程，计数在内存里，每次变化原子写入 `QUOTA_STATE_PATH`（compose 设为 `/app/storage/quota_state.json`，挂载 `./storage`），重启后接着算。状态文件读不了时从 0 计、不拦读取，只记日志。
- 用到 `QUOTA_ALERT_RATIO`（默认 0.8）和用满时各推一次飞书巡检群（`QUOTA_ALERT_WEBHOOK` / `QUOTA_ALERT_SIGN_SECRET`，留空只写日志）；重启后不会重复告警。

## 号池

三家都支持多个 key，逗号分隔配置。账户级失败只冷却出错的那把 key，同一请求立即换下一把；超时、5xx、无正文等页面级失败不烧 key。

| 服务 | 环境变量 | 用法 | 冷却 |
|---|---|---|---|
| Firecrawl | `FIRECRAWL_API_KEYS` | 按配置顺序，前面的优先 | 401 停用到重启，402 24h，429 30s |
| Tavily | `TAVILY_API_KEYS`（兼容 `TAVILY_API_KEY`） | 轮询 | 401 停用，432/433 24h，429 60s |
| Jina | `JINA_API_KEYS`（兼容 `JINA_API_KEY`） | 轮询；不配 key 则匿名请求 | 401 停用，402 24h，403 1h，429 60s |

额度和速率按账号算，同一账号的多个 key 放进号池没有意义。查各 key 余额与状态（不消耗额度、不打印 key）：

```bash
ssh dev 'docker exec reader-service python scripts/check_firecrawl_keys.py'
ssh dev 'docker exec reader-service python scripts/check_tavily_keys.py'
ssh dev 'docker exec reader-service python scripts/check_jina_keys.py'   # account 指纹相同即同账号
```

## 其他配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `READ_TOTAL_TIMEOUT` | 10 | 一次读取的总时限（秒） |
| `DIRECT_HEDGE_DELAY` | 1.5 | 直接抓取多久没结果就请求付费服务（秒） |
| `TAVILY_TIMEOUT` | 8 | Tavily 单独的超时（秒） |
| `JINA_MIN_RETRY_WINDOW` | 4 | Jina 页面级失败后剩余时间不足这么多就不再重试（秒） |
| `DIRECT_READ_ENABLED` | 1 | 设为 0 关闭直接抓取 |
| `DIRECT_READ_TIMEOUT` | 4 | 直接抓取超时（秒） |
| `DIRECT_READ_MIN_CHARS` | 300 | 正文少于这么多字视为没读到（多半是 JS 渲染页或拦截页） |
| `DIRECT_READ_DOH_URL` | `https://223.5.5.5/resolve` | 核验地址用的 DoH |
| `PROVIDER_DAILY_LIMITS` | `firecrawl=300,tavily=300,jina=300` | 各上游每日调用上限，见[每日上游额度](#每日上游额度) |
| `QUOTA_ALERT_RATIO` | 0.8 | 用到上限的这个比例时告警一次 |
| `QUOTA_ALERT_WEBHOOK` / `QUOTA_ALERT_SIGN_SECRET` | 空 | 额度告警推送的飞书巡检群机器人（源值在 dev 的 `~/.config/feishu-webhooks.env` 的 `FEISHU_PATROL_*`）；留空只写日志 |
| `QUOTA_STATE_PATH` | 空（只在内存） | 额度计数的状态文件；compose 已设为 `/app/storage/quota_state.json` |

## 日志

成功是 INFO（`reader_read_succeeded provider=...`），`docker logs` 默认看不到；以下是 WARNING：

- `reader_read_failed kind=... upstream_status=...`：整次读取失败
- `reader_direct_skipped kind=...`：直接抓取没用上（私网地址、正文太短、超时等）
- `reader_firecrawl_page_error page_status=...`：原网页返回错误状态码
- `reader_{firecrawl,tavily}_fallback kind=...`：该方式没读到
- `reader_{firecrawl,tavily,jina}_key_unavailable key_index=... upstream_status=...`：某把 key 进入冷却
- `reader_daily_quota_exceeded provider=... limit=...`：该上游今日额度已用完，本次跳过
- `reader_mcp_unsafe_url domain=... kind=private_dns`：MCP 读之前，域名解析出私网地址，没读
- `reader_mcp_no_content kind=... provider=...`：MCP 读回来了但没有正文（`empty_content` / `access_blocked`）
- `reader_quota_state_{load,save}_failed`、`reader_quota_alert_failed`：额度状态文件读写失败、告警推送失败（只记异常类型和状态码）

## 开发与部署

```bash
python -m unittest -v test_main test_mcp
```

PR 与 push 到 `main` 由仓库根目录的流水线（`.github/workflows/pipeline.yml`）执行单元测试；合入 `main` 且本目录有改动时部署到 dev：`docker compose up -d --build` → `/health` → 真实读取冒烟（`scripts/smoke_read.py`，REST）→ MCP 冒烟（`scripts/smoke_mcp.py`：列工具、一次内网地址、一次真实读取）。

各上游 key 放在 dev 部署目录的 `services/reader/.env`（不入库，变量见上表）。改了 `.env` 后手动重新部署：

```bash
gh workflow run pipeline.yml -R HyxiaoGe/agent-tools -f service=reader
```
