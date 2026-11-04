# flyai

FlyAI 航班 / 高铁查询的适配器：把第三方 CLI 包成 MCP 工具和两个只读 HTTP 接口，服务端持有 API Key，调用方经网关 `/flyai/...` 访问。原为 [Fusion](https://github.com/HyxiaoGe/fusion) 内的 `flyai-adapter`，迁到这里后 Fusion 只带产品密钥。

| 接口 | 说明 |
|---|---|
| `POST /mcp` | MCP（Streamable HTTP，无状态）：工具 `search_flights`、`search_trains`，产品给自己的模型用 |
| `POST /v1/search/flights` | REST 航班查询：`origin`、`destination`、`departure_date` 必填，可选 `cabin_class`、`max_price_yuan`、`departure_hour_start/end`、`sort_by`、`limit`（≤5） |
| `POST /v1/search/trains` | REST 高铁 / 火车查询：参数同上，`cabin_class` 换成 `seat_class` |
| `GET /usage/daily?day=YYYY-MM-DD` | 某天（北京时间，默认今天）启动 CLI 的次数和失败次数，网关日报读取；只留 3 天 |
| `GET /health` | 健康检查，不触发供应商调用 |

## MCP 工具

`search_flights` / `search_trains`：一天、单程、直达，最多返回 5 个班次和查询时的参考价。模型看的说明是英文，两个产品共用；卡片怎么画、回答风格由产品在后面接自己的话。

参数：`origin`、`destination`（城市、机场或车站名）、`departure_date`（不能早于北京时间今天）必填；火车还必填 `train_category`（`high_speed` 只要 G/D/C 字头，`all` 全部）。可选 `cabin_class` / `seat_class`（英文代号，服务里换成飞猪认的中文名）、`departure_after_hour`、`departure_before_hour`、`arrival_before_hour`（出发当天几点前到）、`max_price_yuan`、`sort_by`、`limit`。

结果：`structuredContent` 是整份（`ok`、`observed_at`、`query`、`services`、`limitations`），每个班次有 `option_id`、`number`、航司或车型、出发到达（城市、站、航站楼、当地时刻 `YYYY-MM-DDTHH:MM`）、`arrival.day_offset`、`duration_minutes`、`travel_class`、`price_yuan`、`booking_url`。给模型的文字是同一份 JSON 去掉 `ok` 和预订链接。失败是 `{ok:false, error:{code, message, retryable}}`，文字是给模型的下一步说明，`isError` 保持 false。

| `limitations` | 含义 |
|---|---|
| `filtered_partial_list` | 飞猪那一页（10 条）里只有一部分满足条件，可能还有别的满足条件的班次 |
| `other_class_returned` | 有班次的价格不是所要的舱位 / 座席（查头等舱飞猪常回公务舱） |
| `high_speed_not_covered` | 查全部车型，那一页里一趟 G/D/C 都没有（飞猪默认排序在长线上常只给普速车） |

| 失败代号 | 含义 |
|---|---|
| `invalid_arguments` | 日期是过去、起终点相同、时间条件矛盾等，没调飞猪 |
| `origin_not_recognized` / `destination_not_recognized` | 飞猪认不出的地名会换成别的城市返回（样本里“火星”回了长沙），按起终点核对全对不上就报这个 |
| `no_services` | 没有班次，或有班次但都不满足条件 |
| `provider_no_result` | 飞猪 status 非 0：没有直达和查询出错分不开 |
| `busy` | 排队 6 秒没等到并发槽，可重试 |
| `timeout` / `unavailable` | CLI 超时、出错或返回看不懂 |

几条从 11-04 录的真实样本（`test/fixtures/`）定下来的规矩：

- 先筛后截：条件尽量交给飞猪（舱位座席、时段、价格、到达小时），再在这里按同样的条件核对、排序，最后截到 `limit`。飞猪的小时上限含这个小时，所以“N 点前”传 `N-1`。
- 只要高铁又没指定座席时，带上二等座去查：北京→哈尔滨默认 10 条全是普速车，带二等座就全是 G 字头。
- 时刻原样是当地时间、不补时区（上海→东京 01:10 起飞 04:40 到，时长 150 分钟）；时长最长 7 天（广州→拉萨 Z264 要 53 小时）。
- 县级市的站挂在地级市下（义乌站的城市是金华），站名去掉城市前缀、去掉末尾东西南北也算同一地点；机场按去掉“国际机场”“机场”的名字对（虹桥机场 = 虹桥国际机场）。
- 预订链接只放行飞猪的 `a.feizhu.com`、`router.feizhu.com`（样本里全是后者的短链）。

## 每日上限和计数

- 网关 `SERVICE_DAILY_LIMITS` 里 flyai 默认 100（飞猪免费日包），各产品再用 `PRODUCTS.*.daily_limits` 分。没启动 CLI 的响应（握手、列工具、参数不对、排队超时，以及 REST 的 400、429、503、404、405）都带 `X-Upstream-Calls: 0`，网关据此不计入每日上限。
- `GET /usage/daily` 的字段：`calls`（启动 CLI 次数）、`failed`、`failures`（按代号）、`persistent`（计数是否存盘）。

## 隔离

- CLI 以子进程运行，`detached` 进程组，每次调用独立临时 HOME，结束即删除；超时（20 秒）或请求取消先 SIGTERM 再 SIGKILL。
- 设备标识固定一个：CLI 读 `~/.flyai/device-id`，没有就随机生成；临时 HOME 每次都是新的，不预先放进去就每次都是新设备。标识存在状态目录（`FLYAI_STATE_DIR`，容器里是命名卷 `/var/lib/flyai`），每次调用前写进临时 HOME。
- 容器只读根文件系统、`/tmp` 为 noexec tmpfs、`cap_drop: ALL`、`pids_limit 64`、内存 256M；可写的只有 `/tmp` 和状态卷。
- 自身不鉴权，只在 `agent-tools-internal` 网络里；并发隔离按网关重写的 `X-Caller-App` / `X-Caller-User` 的 sha256 摘要（同一用户同时一个 CLI，全局 2 个，REST 和 MCP 共用），用户标识不落盘不进日志。

## REST 接口的返回

成功返回 `{"observed_at", "request", "items":[...]}`，`items` 只含投影后的安全字段（车次 / 航班号、承运方、出发到达站点与时刻、时长、舱位 / 座席、价格、预订链接）。失败返回 `{"error_code": ...}`：

| 状态 | `error_code` | 含义 |
|---|---|---|
| 400 | `invalid_request` | 参数不合法，或缺少网关身份头 |
| 429 | `concurrency_limited` | 同一用户已有查询在跑，或全局并发已满（2） |
| 502 | `provider_error` / `invalid_provider_response` / `provider_output_too_large` | 供应商 CLI 失败或输出不合法 |
| 503 | `credential_unavailable` | 未配置 `FLYAI_API_KEY` |
| 504 | `provider_timeout` | 供应商 CLI 超过 20 秒 |

## 开发

```bash
npm ci
npm test
```

配置见 `.env.example`。测试不调真实服务，飞猪的返回用 `test/fixtures/` 里录的样本（预订链接换成了假路径）。
