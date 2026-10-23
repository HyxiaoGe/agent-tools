# reader-service

给 Fusion 的 `url_read` 工具读网页正文。输入一个 URL，返回 Markdown 正文；读不到时返回结构化的失败原因，调用方据此告诉模型该来源不可用。

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
| 502 | `upstream_auth` | Jina 号池全部 key 不可用 |
| 502 | `upstream_error` / `request_error` | Jina 返回其他错误或请求失败 |
| 422 | — | URL 格式无效 |

### `GET /health`

只看各号池状态，不调用上游、不消耗额度。某个号池的 key 全部不可用（密钥无效、额度用完或限流）时返回 503，正文里按 `jina_account` / `tavily_account` / `firecrawl_account` 给出序号和状态码（不含 key），dev-ops-sentinel 据此告警；冷却到期自动恢复。

## 读取调度

一次读取的总时限 10s（`READ_TOTAL_TIMEOUT`），超时即放弃该网页：Fusion 会并行读多个网页、等最慢的那个，读不到的要尽快放弃。

1. **0s：直接抓取**（`direct_read.py`）。DoH 解析并核验全部是公网地址后直连，trafilatura 提取正文。免费，成功即返回。
2. **1.5s 还没成功**（`DIRECT_HEDGE_DELAY`，直接抓取先失败则立即）：**同时请求 Firecrawl 和 Tavily**，直接抓取继续跑，谁先读到正文用谁。
3. **Firecrawl 报原网页 404/410**：立即结束，返回 `page_not_found`，不再等其他方式。
4. **Jina 只兜底**：Firecrawl 失败（超时、被拦截、原网页 403 等其他错误码、无正文、号池全部不可用）后才启动；没配 Firecrawl 时第 2 步直接带上 Jina。Jina 会把错误页包装成 200 返回，这里按它头部的 `Warning: Target URL returned error <状态码>` 识别，错误页不当正文。

为什么 Firecrawl 为主：同一批 120 个网址实测，Firecrawl 中位 4.3s、Jina 8.6s，两者读到真正正文的页数相当；Firecrawl 会报告原网页状态码，Jina 读到的页里约一成是 404 等错误页。

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

## 日志

成功是 INFO（`reader_read_succeeded provider=...`），`docker logs` 默认看不到；以下是 WARNING：

- `reader_read_failed kind=... upstream_status=...`：整次读取失败
- `reader_direct_skipped kind=...`：直接抓取没用上（私网地址、正文太短、超时等）
- `reader_firecrawl_page_error page_status=...`：原网页返回错误状态码
- `reader_{firecrawl,tavily}_fallback kind=...`：该方式没读到
- `reader_{firecrawl,tavily,jina}_key_unavailable key_index=... upstream_status=...`：某把 key 进入冷却

## 开发与部署

```bash
python -m unittest test_main
```

没有 PR CI。push 到 master 触发 self-hosted 部署（`.github/workflows/deploy.yml`）：单元测试 → `docker run`（key 从仓库 Secret 注入）→ `/health` → 真实读取冒烟（`scripts/smoke_read.py`）。改了 Secret 要手动重新部署才生效：

```bash
gh workflow run deploy.yml -R HyxiaoGe/reader-service
```
