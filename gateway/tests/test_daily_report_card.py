from datetime import date

from app.daily_report import DayStats, ServiceStats, render_report_card


def test_card_keeps_historical_failures_compact_and_nonurgent():
    stats = DayStats(date(2026, 11, 4), calls=217, failed=6)
    stats.services = {
        "weather": ServiceStats(calls=75),
        "search": ServiceStats(calls=74),
        "maps": ServiceStats(calls=25),
        "flyai": ServiceStats(calls=13, failed=4),
        "reader": ServiceStats(calls=12, failed=2),
    }
    stats.products.update({"lingdraft": 208, "fusion": 8, "validation": 1})
    card = render_report_card(
        stats, None, {"available": True, "providers": [], "firecrawl_remaining_credits": 69491}, {}
    )
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "blue"
    text = str(card)
    assert "217" in text and "97.2%" in text
    assert "flyai：失败 4/13" in text and "reader：失败 2/12" in text
    assert "69,491" in text and "昨日回顾" in text
    assert "tools/call" not in text and len(card["card"]["elements"]) <= 6


def test_missing_upstream_is_shown_without_fake_zero():
    stats = DayStats(date(2026, 11, 4))
    card = render_report_card(stats, None, None, {"reader": {"available": False}})
    text = str(card)
    assert "search：上游数据未取到" in text
    assert "reader：上游数据未取到" in text
    assert "暂无有效的每日额度计数" in text
    assert "成功率" in text and "—" in text


def test_top_three_attention_items_with_overflow_and_no_disabled_provider_noise():
    stats = DayStats(date(2026, 11, 4), calls=20, failed=10)
    stats.services = {f"svc-{i}": ServiceStats(calls=2, failed=1) for i in range(10)}
    card = render_report_card(stats, None, {"available": True, "providers": []}, {})
    detail = card["card"]["elements"][2]["text"]["content"]
    assert detail.count("• ") == 3 and "另有 7 项" in detail


def test_daily_quota_selects_largest_real_usage_ratio():
    stats = DayStats(date(2026, 11, 4), calls=10)
    search = {
        "available": True,
        "providers": [
            {"provider": "deepseek", "used": 69, "limit": 1000},
            {"provider": "unused", "used": 0, "limit": 0},
        ],
    }
    image = {"available": True, "providers": [{"provider": "volcano", "used": 9, "limit": 100}]}
    card = render_report_card(stats, None, search, {"image": image})
    text = str(card)
    assert "image/volcano 9/100（9%）" in text
    assert "unused" not in text


def test_upstream_fallbacks_and_maps_quota_are_kept_in_attention():
    stats = DayStats(date(2026, 11, 4), calls=10)
    search = {"available": True, "providers": [], "fallback": {"error": 2, "insufficient": 8}}
    maps = {"available": True, "providers": [{"provider": "amap", "quota": 1, "skipped": 2}]}
    card = render_report_card(stats, None, search, {"maps": maps})
    text = str(card)
    assert "search：转备用 10 次" in text
    assert "maps：上游额度用尽 1 次，跳过 2 次" in text
