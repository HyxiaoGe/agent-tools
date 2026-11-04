# maps

地图组合层。产品不持有地图 key，经网关 `/maps/mcp` 拿两个 MCP 工具给自己的模型：`place_search`（搜地点）和 `route_compare`（比较几种出行方式）。服务直接调高德、腾讯的 Web 服务 REST，先高德，高德不行时转腾讯。

2026-11-04 从 Fusion 挪过来（Fusion `app/services/mcp/amap_product_tools.py`），Fusion 和灵稿共用。原来的 `amap-service`、`tmap-service` 两个代理不动，Fusion 还在用；Fusion 什么时候改用这里由 Fusion 那边定。

## 为什么直接调 REST

- 高德 MCP 搜索不给评分、人均消费、营业时间，Fusion 对前 3 条再查详情（APT:646），一次搜索占 4 次额度；REST 一次搜索就全有（`show_fields=business,photos`），省掉这 3 次。
- 腾讯 MCP 回给人看的中文段落，各工具写法不一样；REST 是 JSON。
- 两家都是 GCJ-02，同一次调用里可以混用（高德解析的起点 + 腾讯算的路线）。

## 什么时候转腾讯

按每一次上游请求判断，不是整次调用一起转：

- 转：额度用尽（高德 10003、10044 等，腾讯 121）、超每秒上限、超时、连不上、5xx、返回格式不对、key 无效或没开权限（另记错误日志 `maps_auth_failed`）、高德做不了（算公交缺城市）。
- 不转：上游拒了这次请求本身（参数不对、路线太远、地址解析没结果）、查到 0 条、地名重名。换一家多半也不行，还白占额度。
- 额度用尽后记住这一家的这组接口，到恢复时间前不再请求，但最多跳 1 小时（腾讯在控制台调配额后会恢复）。高德额度按月，下月 1 日恢复；腾讯按天，次日零点恢复。重启就忘。
- 每家同时最多 3 个请求；每次上游请求 8 秒，不重试；整次工具调用 25 秒，路线到时已算出的照样返回。

腾讯比高德少的：没有评分、人均消费、营业时间、图片、跳转链接；搜索不支持“或”，只搜第一个词（`limitations` 标 `first_term_only`）。

## place_search

参数（模型填）：

- `query`：搜什么，1–80 字。几个词用 `|` 或逗号分开。用户限定了区就带上。
- `anchor_source`：`named` 在 `near` 附近搜；`current_location` 在设备当前位置附近搜；`none` 在 `city` 里按文字搜（不给城市就全国）。
- `city`、`near`（地名，不收坐标）、`near_candidate`（重名后带候选号再查）、`radius_m`（100–50000，默认 3000）、`limit`（1–5，默认 5）。

结果：

```json
{"ok": true, "status": "success", "query": "咖啡", "city": "上海", "result_count": 1,
 "anchor": {"source": "named", "label": "外滩", "city": "上海市"},
 "places": [{"place_id": "amap_B00155QFWB", "name": "Costa Coffee(上海外滩店)", "address": "中山东一路387号",
             "district": "黄浦区", "city": "上海市", "category": "餐饮服务;咖啡厅;咖啡厅", "distance_m": 54,
             "rating": 4.3, "reference_cost_yuan": 40, "open_hours": "...", "business_area": "北京路",
             "photo_url": "https://store.is.autonavi.com/...", "map_url": "https://uri.amap.com/marker?poiid=B00155QFWB&src=lingdraft&callnative=0",
             "provider": "amap"}],
 "limitations": [], "providers": ["amap"]}
```

- `place_id` 带服务商前缀，模型拿它去 `route_compare` 定起终点。
- `map_url` 只有高德的地点有，`src` 是网关转来的调用方（`x-caller-app`）。腾讯的跳转链接要带的 referer 文档说不清，不给。
- 没有坐标（Fusion 0909d20f）。

## route_compare

参数：`origin`、`destination`（地名，或 `current_location`）、`origin_source`、`destination_source`（`named` / `current_location`）、`modes`（driving、transit、walking、bicycling 里 1–3 个），可选 `origin_city`、`destination_city`、`origin_candidate`、`destination_candidate`、`origin_place_id`、`destination_place_id`、`requested_departure_time`（只记用户原话，不按那个时刻算）。

结果：

```json
{"ok": true, "status": "success",
 "origin": {"source": "named", "label": "虹桥火车站", "city": "上海市"},
 "destination": {"source": "named", "label": "外滩", "city": "上海市"},
 "routes": [{"mode": "driving", "distance_m": 28251, "duration_s": 6181, "summary": "速度最快", "toll_yuan": 0, "provider": "amap"},
            {"mode": "transit", "duration_s": 3380, "fare_yuan": 5, "transit_type": "subway", "transfers": 0,
             "walking_distance_m": 1023,
             "legs": [{"kind": "subway", "line_name": "地铁10号线(虹桥火车站--基隆路)", "departure_stop": "虹桥火车站",
                       "arrival_stop": "南京东路", "via_stop_count": 14, "entrance": "南1入口", "exit": "6号口",
                       "distance_m": 19402, "duration_s": 2220}],
             "alternatives": [], "provider": "amap"}],
 "unavailable_modes": [], "limitations": [], "providers": ["amap"]}
```

- 公交没有总距离。`status` 为 `degraded` 时有的方式没算出来，见 `unavailable_modes`。
- 腾讯的用时以分钟计、票价以分计，这里换成秒和元；途经站数换成和高德一样的算法。

## 地名怎么定

两家一套规则，照 Fusion（见 `app/resolve.py`；关键词兜底 Fusion 只用在路线起终点，这里查地点的 near 也用）：先看地名解析的候选，只有一个就用；好几个先按城市筛（终点没给城市用起点的城市），还剩好几个就回 `ambiguous_location`，带最多 5 个编号候选，模型带上编号再查。解析不出来再按关键词搜索，从结果里按名称挑。腾讯的地名解析用输入提示（一次 10 个带省市区），地址解析一次只回一个，看不出重名。

## 当前位置

产品在 MCP 请求的 `_meta["agent-tools/geolocation"]` 里传设备位置：`{"latitude", "longitude", "accuracy_m"}`（WGS84，和浏览器定位一样；精度超过 50 公里的不用）。服务先换成 GCJ-02（高德坐标转换，腾讯兜底）。没传就回 `location_unavailable`。位置不在模型参数里，模型编不出来。

## 失败

`structuredContent` 是 `{"ok": false, "error": {"code", "message", "retryable", ...}}`，`isError` 为 false。给模型的文字：

- `ambiguous_location`（`retryable: true`）：带 `candidate_argument` 和编号候选（城市、区、匹配级别或分类、坐标），说明四种情况怎么办；坐标只给模型自己判断。
- `place_not_found`：找不到这个地名，能从对话补城市就再查，否则问用户。Fusion 这里报的是工具不可用。
- `place_id_unresolved`：编号查不到，用搜索结果里的编号或改用地名。
- `location_unavailable`：没有当前位置，问用户要地名。
- `no_route`：几种方式都算不出（太远、没公交）。
- `invalid_arguments`：参数哪里不对。
- `quota_exhausted`（带 `reset_at`）、`unavailable`、`timeout`：只告诉模型没有可用结果，不让它把工具状态讲给用户。

## 用量

`GET /usage/daily?day=YYYY-MM-DD`（北京时间，默认今天）：

```json
{"day": "2026-11-04", "available": true, "fallbacks": 2,
 "providers": [{"provider": "amap", "requests": 10, "failed": 0, "quota": 1, "skipped": 1}]}
```

`requests` 发出去的请求，`failed` 可以转的失败（额度用尽另计在 `quota`），`skipped` 记着用尽没发的，`fallbacks` 转给下一家的次数。存在 `storage/usage.json`，留 3 天，只有计数，没有地名。网关日报读这里。

## 运行

```bash
cp .env.example .env     # 填两把 key
uv sync --frozen && uv run python -m app.main
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

测试用 `tests/fixtures` 里录好的两家响应（11-04 用真 key 录的，去掉了路线折线），不打真实接口。部署后冒烟：`docker exec -i maps-service python - < scripts/smoke_maps.py`（高德 5 次、腾讯 1 次）。
