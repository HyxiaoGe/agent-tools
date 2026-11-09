# playground

agent-tools 的工具目录与在线试用页。八类能力、十一个开放操作；搜索结果可以继续读取全文，保留来源列表。独立 FastAPI 服务托管原生 HTML / CSS / JavaScript，不需要前端构建，也不调用 LLM 生成解释。

表单使用统一的自定义菜单、分段选择、比例与质量卡片、开关、日历组件，保留标准表单提交。菜单支持方向键、Home / End、Tab 确认与 Esc 取消；日历支持方向键、按月或年翻页、手动日期输入和焦点返回。弹层在工作区外的顶层显示，避免被容器边界截断。

每个操作首次打开时载入第一套完整示例，必填内容与筛选项均可直接提交。出行示例默认北京时间明天，天气未指定日期时自动查询近期预报。点击示例会恢复该示例的全部参数（包含清除旧的可选筛选），不自动调用或扣减次数；界面标明当前示例，修改参数后取消高亮。切换操作保留自定义值，包括主动清空的输入。

结果区默认展示每个操作的一份固定示例，可切换「示例预览 / 真实结果」。示例资料与灯塔样例图随服务打包，未配置密钥或额度用完仍能浏览，不访问网关、不扣次数。搜索示例的「查看示例正文」同样使用内置资料。示例明确标记为固定演示、不随左侧参数变化、不代表实时信息；灯塔图是历史生成样例。点击「运行工具」才使用当前表单真实调用，完成后自动切换到真实结果，再看示例也会保留真实结果。GET `/api/examples` 只返回内置资料和经相同 Markdown 清理的正文，不返回凭证。

## 本地运行

```bash
cp .env.example .env
uv sync --frozen
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8101 --no-access-log
```

访问 `http://127.0.0.1:8101`。未配置密钥时仍可浏览完整目录与标明来源的内置示例，真实调用按钮不可用；`/api/run` 不返回模拟成功结果。

要启用真实调用，在服务端配置 `PLAYGROUND_GATEWAY_URL` 和 `PLAYGROUND_API_KEY`。按当前约定，页面复用已有 Fusion 产品密钥；所有访客共用后端凭据，不需要填写密钥，浏览器也拿不到它。密钥仅保存在部署环境的 `.env` 或进程环境中，不入库、不打印。本地预览可通过 SSH 安全读取到进程内存，不在本地落盘。

## 共用产品密钥

将已有 Fusion 产品的网关密钥配置为页面后端的 `PLAYGROUND_API_KEY` 即可，无需新增网关产品或修改工具服务的授权。页面只开放自身白名单中的十一个操作，不会将该产品全部服务接口开放给浏览器。

共享时，网关把调用记到 Fusion 产品名下，场景为 `playground`，共用 Fusion 的服务权限、每日额度和生图归因。页面自身的访客限流仍然生效，不影响其他产品的配置。

## 可选：单独统计试用产品

如果以后需要独立的产品统计与每日总额，可在网关 `PRODUCTS` 中登记 `playground` 产品。下面只是可选配置片段，需要与已有产品合并，不能覆盖整个 `PRODUCTS`：

```json
{
  "playground": {
    "service_key_env": "PLAYGROUND_SERVICE_KEY",
    "services": ["search", "reader", "image", "trends", "maps", "weather", "flyai", "context7"],
    "daily_limits": {"search": 100, "reader": 100, "image": 10, "trends": 100, "maps": 30, "weather": 50, "flyai": 10, "context7": 50}
  }
}
```

网关的 `PLAYGROUND_SERVICE_KEY` 与本服务的 `PLAYGROUND_API_KEY` 使用同一把专用密钥。若开放生图，还须在 image-service 的产品配置中登记相同身份与凭证，沿用其 [产品归因配置](../image/docs/PRODUCT_ATTRIBUTION.md)，否则只有生图会被拒绝。此配置涉及现有部署与额度分配，开发代码不会自动写入。

访问路径：浏览器 → playground → gateway → 原有工具。playground 只加入 `middleware_default`，不连接工具专用内网，不绕过网关鉴权和记账。网关重写身份头，按密钥所属产品记录用量；试用流量仍受共享的服务总额度与上游额度约束。

## 开放操作

| 能力 | 操作 | 实际入口 |
| --- | --- | --- |
| 联网搜索 | `search_web` | `/search/mcp` |
| 网页读取 | `read_page` | `/reader/mcp` |
| 图像生成 | `generate_image` | REST `/image/v1/generate` |
| 热榜资讯 | `get_latest_news` | `/trends/mcp` |
| 地图与地点 | `place_search`、`route_compare` | `/maps/mcp` |
| 天气预报 | `get_weather` | `/weather/mcp` |
| 航班与铁路 | `search_flights`、`search_trains` | `/flyai/mcp` |
| 技术文档 | `resolve-library-id`、`query-docs` | `/context7/mcp` |

高德和腾讯的独立代理也已集成在仓库中，页面展示的地点与路线试用通过 maps 组合层调用。目录展示的是代码集成情况，不宣称所有线上服务实时健康。天气、交通的歧义与失败、来源切换、额度等业务规则继续由现有服务处理；页面不重新实现。

## 试用边界

- API 只接收注册操作和经过 Pydantic 校验的参数；不提供任意 URL 代理、任意 MCP 方法、上传或删除接口。用户网页地址仅交给已有 `read_page`，保留其 DNS、重定向与 SSRF 校验。
- 每个客户端地址每分钟默认 12 次、每天 60 次、生图每天 3 次，同一地址最多一个正在运行的调用，全站最多 8 个。最多记录 10000 个当日访客，超过返回忙碌。访客标识为加盐散列，不把 IP 送给工具。
- 试用入口全站每天默认最多 100 次调用、其中生图最多 10 次，分别通过 `PLAYGROUND_SITE_REQUESTS_PER_DAY`、`PLAYGROUND_SITE_IMAGES_PER_DAY` 配置。换 IP 也不能绕过全站总额；不改变共享 Fusion 产品自身的授权与额度。
- 每日个人与全站次数存入 SQLite（默认 `storage/playground.sqlite3`），匿名标识所用的盐同库持久化，刷新页面、重启或重建容器都不会重置当天计数；compose 的 `playground-data` 命名卷保留该目录。北京时间零点自动开始新一天，不需要删除历史计数。计数库不可用时返回 503，不继续调网关。
- 在转发前同时预占个人与全站次数，SQLite 事务保证并发请求不超额。参数检查失败不计数，已经提交的调用即使超时或失败也计数、不自动退款，避免反复失败调用消耗共享额度。搜索和读取全文分别计一次；生图同时计入总调用和生图次数。
- 页面显示个人与全站剩余额度，成功或失败后更新，额度用尽禁用相关运行按钮，后端仍独立返回 429 阻止直接调用。查看额度、刷新页面不计数。`GET /api/quota` 仅返回调用者的匿名汇总与全站总量，不提供其他人的明细。
- 频率与并发仍是单进程内存状态，必须保持单 worker；网关的产品上限与上游额度继续生效。页面后端不信任浏览器自带的身份头或 `X-Forwarded-For`。共用 NAT 的访客会共用限制；默认代理下可能全部共用一个限制。需要真实访客区分时，仅在 Uvicorn 显式信任受控反向代理的地址，不设 `--forwarded-allow-ips='*'`。
- 输入最大 16 KiB，上游返回最大 2 MiB。每次 MCP 调用独立初始化会话并尽力关闭，不共享访客的会话。
- `POST /api/run` 仅接收 JSON，校验同源来源，不开放 CORS。反向代理部署时配置 `PLAYGROUND_PUBLIC_ORIGIN` 为页面的 HTTPS 来源。
- 不自动运行工具，不自动重试。切换工具保留参数与结果；读取失败保留搜索列表和原文入口。关闭正文面板不会承诺取消已提交的上游调用，重新打开仍须等待当前调用完成。
- 网页 Markdown 禁用原始 HTML，并经 nh3 清理，远程图片不自动加载。前端其他返回数据通过 DOM 文本节点展示。浏览器仅可向同源发请求，产品密钥留在后端；接口不回传上游响应头与异常详情。
- 生图沿用现有公开图片；REST 返回的 `/static/images/<UUID>.<格式>` 相对地址由页面经同源 `/api/images/<文件名>` 读取固定网关静态目录。只接受 UUID 文件名与 PNG / JPEG / WebP，最多 20 MiB、不跟随重定向，不接受任意 URL。页面不新增图片存储或公开机制；读取原图、重新加载预览不生成新图、不扣试用次数。加载失败会明确提示，保留原图入口。如不希望开放生图，将访客生图额度设为 0。

## 部署准备

发布地址为 `https://agent-tools.seanfield.org`。服务在 `8101` 监听，compose 仅绑定 `127.0.0.1:8101`。沿现有流水线构建与部署；反向代理将该域名指向 `agent-tools-playground:8101`，保留同源路径 `/api/*` 与 `/assets/*`。这里不占用网关现有域名或路由。反代模板见 `deploy/nginx-playground.conf`，域名经现有香港中继和 SSH 反向隧道进入 dev；HTTPS 证书复用现有 webroot 签发与续期。

部署 `.env` 设置 `PLAYGROUND_PUBLIC_ORIGIN=https://agent-tools.seanfield.org`，并将 `PLAYGROUND_TRUSTED_PROXY_IP` 设置为 nginx-proxy 在 middleware_default 上的实际地址（可通过 `docker inspect nginx-proxy` 核对）。compose 将它传给 Uvicorn 的 `FORWARDED_ALLOW_IPS`；只信任该代理传入的客户端地址，代理覆盖访客自行提交的 X-Forwarded-For。Nginx 容器网络地址变化后应同步该配置并重建页面容器，不能使用 `*` 代替。计数 SQLite 放在命名卷中，正常发布不删除该卷。

`GET /health` 只检查页面服务是否运行，并返回 `configured`，不探测工具上游、不消耗额度。`configured: true` 仅表示已填产品密钥，真实凭据有效性需逐项调用验证。对外开放前，应确认共享产品的权限与额度，配置反向代理与 HTTPS。

## 验证

```bash
uv run --frozen pytest -q
ruff check .
ruff format --check .
node --check app/static/app.js
node --check app/static/components.js
```

接口测试使用 HTTP mock，覆盖搜索到读取的真实路由流程、JSON / SSE、会话、失败与额度、输入边界、限流、密钥隔离和 Markdown 安全。它们不代表已通过线上供应商验收。

`tests/ui_controls.js` 是 Playwright CLI 函数表达式，针对连接 `tests.fake_gateway` 的 `http://127.0.0.1:18101` 测试页运行，脚本会拒绝真实预览端口。先在 18100 启动测试网关，再以测试密钥 `test-only`、该网关地址在 18101 启动页面，最后使用 CLI 的 `run-code --filename=tests/ui_controls.js`。它检查键盘选择、参数实际提交、月末与闰年、开关与多选、375 / 768 / 1440 像素页面及弹层边界，不消耗供应商额度。该文件作为表达式载入，首尾不使用独立分号。

`tests/ui_quota.js` 检查计数显示、扣减、刷新后保持为零、按钮禁用和直接请求被拒绝。它同样只允许 18101 测试页；为它设置个人总额 3、生图 1、全站总额 4、生图 2，并使用独立的全新 `PLAYGROUND_QUOTA_DB_PATH`。计数从启用时开始记录，不回填启用之前的调用。

`tests/ui_defaults.js` 核对全部十一个操作的初始参数、示例完整性与实际提交，并检查示例恢复数量、时间范围、生图比例和质量、路线方式、日期和车型等筛选，以及清空输入后切换操作的行为。它同样只访问 18101 测试页；使用独立计数库并提高测试额度，不消耗真实工具额度。

`tests/ui_images.js` 使用与真实 REST 相同的相对图片地址，检查预览、打开原图、加载失败后重试，以及多次读取只消耗一次生图调用。运行方式与默认值回归相同，只访问本地测试网关。

`tests/ui_examples.js` 检查全部操作的示例标识和内容、打包图片、示例正文、零调用与计数不变、真实结果保留，以及额度耗尽或未配置时仍能预览；还覆盖 375 / 768 / 1440 像素下的示例布局。运行方式同上。

MCP 响应同时支持原生 `structuredContent`、文本 JSON，以及 FastMCP 对字符串返回值添加的 `structuredContent: {"result": "…"}` 外层包装。解包后仍逐项校验工具的数据契约，不把缺少字段或格式错误的结果当成功。热榜查询默认请求来源 URL，展示平台中文名称、名次和按北京时间解释的抓取时间。`tests/test_trends.py` 覆盖实际包装、直接 JSON、纯文本、无效响应及失败状态；`tests/ui_trends.js` 使用纽约浏览器时区核对新闻卡片、来源链接和北京时间，运行方式同上。
