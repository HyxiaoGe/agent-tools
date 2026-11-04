"""latest_news：取每个平台最近一批在榜的条目，不是当天所有上过榜的。

库按 TrendRadar 4.1.1 的建表语句造（只建用到的三张表），一次 crawl() 模拟一次抓取：
在榜的按顺序记名次，之前在榜、这次不在的记 0；没写进这次抓取的平台当作这次没抓成。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from latest_news import latest_news, read_latest_batch

SCHEMA = """
CREATE TABLE platforms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    url TEXT DEFAULT '',
    mobile_url TEXT DEFAULT '',
    first_crawl_time TEXT NOT NULL,
    last_crawl_time TEXT NOT NULL,
    crawl_count INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (platform_id) REFERENCES platforms(id)
);
CREATE TABLE rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    crawl_time TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_item_id) REFERENCES news_items(id)
);
"""

NOW = datetime(2026, 11, 4, 22, 40)
YESTERDAY = datetime(2026, 11, 3, 22, 40)


class Day:
    """一天的库。"""

    def __init__(self, path: Path, platforms=(("weibo", "微博"), ("zhihu", "知乎"))):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.executemany("INSERT INTO platforms (id, name) VALUES (?, ?)", platforms)
        self.conn.commit()

    def crawl(self, time: str, lists: dict[str, list[str]]) -> None:
        for platform, titles in lists.items():
            on_list = set()
            for rank, title in enumerate(titles, start=1):
                row = self.conn.execute(
                    "SELECT id FROM news_items WHERE platform_id = ? AND title = ?", (platform, title)
                ).fetchone()
                if row is None:
                    news_id = self.conn.execute(
                        "INSERT INTO news_items (title, platform_id, rank, url, mobile_url,"
                        " first_crawl_time, last_crawl_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (title, platform, rank, f"https://{platform}.example/{title}", "", time, time),
                    ).lastrowid
                else:
                    news_id = row[0]
                    self.conn.execute(
                        "UPDATE news_items SET rank = ?, last_crawl_time = ? WHERE id = ?", (rank, time, news_id)
                    )
                self.conn.execute(
                    "INSERT INTO rank_history (news_item_id, rank, crawl_time) VALUES (?, ?, ?)",
                    (news_id, rank, time),
                )
                on_list.add(news_id)
            for (news_id,) in self.conn.execute(
                "SELECT id FROM news_items WHERE platform_id = ?", (platform,)
            ).fetchall():
                if news_id not in on_list:
                    self.conn.execute(
                        "INSERT INTO rank_history (news_item_id, rank, crawl_time) VALUES (?, 0, ?)", (news_id, time)
                    )
        self.conn.commit()


@pytest.fixture
def today(tmp_path):
    return Day(tmp_path / "2026-11-04.db")


def only_today(day):
    def db_path_for(when):
        return day.path if when.date() == NOW.date() else None

    return db_path_for


def brief(items):
    return [(i["platform"], i["rank"], i["title"]) for i in items]


def test_names_and_ranks_come_from_the_latest_crawl_not_the_first(today):
    today.crawl("09-01", {"weibo": ["早上第一", "后来升上去", "掉下去的"]})
    today.crawl("22-31", {"weibo": ["新上来的", "早上第一", "后来升上去"]})

    items = read_latest_batch(today.path, NOW, None, include_url=False)

    # 上游会给出“早上第一”第 1、“掉下去的”第 3；现在的榜单里“掉下去的”已经没了
    assert brief(items) == [("weibo", 1, "新上来的"), ("weibo", 2, "早上第一"), ("weibo", 3, "后来升上去")]
    assert {i["timestamp"] for i in items} == {"2026-11-04 22:31:00"}


def test_a_platform_that_failed_this_crawl_keeps_its_previous_batch_and_time(today):
    today.crawl("22-01", {"weibo": ["微博旧"], "zhihu": ["知乎一", "知乎二"]})
    today.crawl("22-31", {"weibo": ["微博新"]})  # 知乎这次没抓成

    items = read_latest_batch(today.path, NOW, None, include_url=False)

    assert [(i["platform"], i["title"], i["timestamp"][11:16]) for i in items] == [
        ("weibo", "微博新", "22:31"),
        ("zhihu", "知乎一", "22:01"),
        ("zhihu", "知乎二", "22:01"),
    ]


def test_interleaves_by_rank_then_platform_order_and_cuts_to_limit(today):
    today.crawl("22-31", {"zhihu": ["知乎一", "知乎二", "知乎三"], "weibo": ["微博一", "微博二", "微博三"]})

    items = latest_news(only_today(today), NOW, None, 4, include_url=False)

    # 同名次按平台表的顺序（微博在前），和上游一样跨平台交错
    assert brief(items) == [
        ("weibo", 1, "微博一"),
        ("zhihu", 1, "知乎一"),
        ("weibo", 2, "微博二"),
        ("zhihu", 2, "知乎二"),
    ]


def test_platform_filter(today):
    today.crawl("22-31", {"zhihu": ["知乎一"], "weibo": ["微博一"]})

    assert brief(latest_news(only_today(today), NOW, ["zhihu"], 10, include_url=False)) == [("zhihu", 1, "知乎一")]


def test_urls_only_when_asked(today):
    today.crawl("22-31", {"weibo": ["微博一"]})

    without = latest_news(only_today(today), NOW, None, 10, include_url=False)
    with_url = latest_news(only_today(today), NOW, None, 10, include_url=True)

    assert set(without[0]) == {"title", "platform", "platform_name", "rank", "timestamp"}
    assert without[0]["platform_name"] == "微博"
    assert with_url[0]["url"] == "https://weibo.example/微博一"
    assert with_url[0]["mobileUrl"] == ""


def test_falls_back_to_yesterday_before_the_first_crawl_of_the_day(tmp_path):
    yesterday = Day(tmp_path / "2026-11-03.db")
    yesterday.crawl("23-31", {"weibo": ["昨晚第一"]})
    paths = {NOW.date(): None, YESTERDAY.date(): yesterday.path}

    items = latest_news(lambda when: paths[when.date()], NOW, None, 10, include_url=False)

    assert brief(items) == [("weibo", 1, "昨晚第一")]
    assert items[0]["timestamp"] == "2026-11-03 23:31:00"


def test_an_empty_today_database_also_falls_back(tmp_path):
    today = Day(tmp_path / "2026-11-04.db")  # 抓取端建了库还没写进数据
    yesterday = Day(tmp_path / "2026-11-03.db")
    yesterday.crawl("23-31", {"weibo": ["昨晚第一"]})
    paths = {NOW.date(): today.path, YESTERDAY.date(): yesterday.path}

    assert brief(latest_news(lambda when: paths[when.date()], NOW, None, 10, include_url=False)) == [
        ("weibo", 1, "昨晚第一")
    ]


def test_no_data_in_either_day_returns_empty():
    assert latest_news(lambda when: None, NOW, None, 10, include_url=False) == []


def test_reads_without_writing(today):
    today.crawl("22-31", {"weibo": ["微博一"]})
    today.conn.close()
    today.path.chmod(0o444)  # 容器里 output 是只读挂载

    assert brief(read_latest_batch(today.path, NOW, None, include_url=False)) == [("weibo", 1, "微博一")]
