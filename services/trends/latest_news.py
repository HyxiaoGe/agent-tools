"""热榜的“最新一批”：每个平台最近一次抓取时在榜的条目，名次是那一次的名次。

上游 DataService.get_latest_news（mcp_server/services/data_service.py:56-128）读的是当天所有上过榜的
标题，名次取当天第一次上榜时的（:102），每条都标最新一次抓取的时间（:109），再按这个名次排。
越到晚上差得越多：按 11-04 的库、照 Fusion 每日灵感那样取前 60 条，05:00 有 7 条已经不在榜上，
09:00 有 27 条，22:30 有 47 条。contract_server.py 用这里的实现替换它，工具名、参数、返回格式不变。

数据是 TrendRadar 每次抓取写的 SQLite（output/news/<日期>.db）：rank_history 每次抓取给在榜的
每条记一个名次，掉榜的记 0。某个平台这次没抓成就没有它的行，用它上一次抓到的那一批，时间也标那一次的。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

# 抓取端写库时会锁住整个库（rollback journal），读的时候最多等这么久
BUSY_TIMEOUT_SECONDS = 5

_LATEST_BATCH_SQL = """
WITH latest AS (
    SELECT n.platform_id, MAX(h.crawl_time) AS crawl_time
    FROM rank_history h JOIN news_items n ON n.id = h.news_item_id
    WHERE h.rank > 0 {platform_filter}
    GROUP BY n.platform_id
)
SELECT n.platform_id, COALESCE(p.name, n.platform_id), n.title, h.rank, n.url, n.mobile_url,
       l.crawl_time, p.rowid
FROM latest l
JOIN news_items n ON n.platform_id = l.platform_id
JOIN rank_history h ON h.news_item_id = n.id AND h.crawl_time = l.crawl_time AND h.rank > 0
LEFT JOIN platforms p ON p.id = n.platform_id
"""


def _timestamp(day: datetime, crawl_time: str) -> str:
    """crawl_time 是 TrendRadar 记的“HH-MM”，换成和上游一样的“YYYY-MM-DD HH:MM:SS”。"""
    hour, _, minute = crawl_time.partition("-")
    return f"{day:%Y-%m-%d} {hour}:{minute}:00"


def read_latest_batch(db_path: Path, day: datetime, platforms: Sequence[str] | None, include_url: bool) -> list[dict]:
    """一天的库里每个平台最近一批在榜的条目：先按名次，同名次按平台在配置里的顺序。"""
    platform_filter = ""
    params: list[str] = []
    if platforms:
        platform_filter = f"AND n.platform_id IN ({','.join('?' for _ in platforms)})"
        params = list(platforms)
    conn = sqlite3.connect(
        f"{Path(db_path).resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=BUSY_TIMEOUT_SECONDS,
    )
    try:
        rows = conn.execute(_LATEST_BATCH_SQL.format(platform_filter=platform_filter), params).fetchall()
    finally:
        conn.close()

    # 平台表里没有的排在最后
    rows.sort(key=lambda r: (r[3], r[7] if r[7] is not None else float("inf"), r[0]))
    items = []
    for platform, name, title, rank, url, mobile_url, crawl_time, _ in rows:
        item = {
            "title": title,
            "platform": platform,
            "platform_name": name,
            "rank": rank,
            "timestamp": _timestamp(day, crawl_time),
        }
        if include_url:
            item["url"] = url or ""
            item["mobileUrl"] = mobile_url or ""
        items.append(item)
    return items


def latest_news(
    db_path_for: Callable[[datetime], Path | None],
    now: datetime,
    platforms: Sequence[str] | None,
    limit: int,
    include_url: bool,
) -> list[dict]:
    """最新一批，按名次交错、取前 limit 条。

    今天的库还没有（刚过零点、抓取还没跑）就用昨天最后一批。两天都没有返回空列表，
    由调用方报“没有数据”，不拿空列表冒充没有热点。
    """
    for day in (now, now - timedelta(days=1)):
        path = db_path_for(day)
        if path is None:
            continue
        items = read_latest_batch(path, day, platforms, include_url)
        if items:
            return items[:limit]
    return []
