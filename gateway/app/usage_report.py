"""按产品 / 服务 / 场景 / MCP 工具汇总网关调用：python -m app.usage_report [--app-id fusion] [--days 30]"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from app.config import Settings


def summarize(db_path: str, *, app_id: str | None, days: int) -> dict:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where, params = "ts >= ?", [since]
    if app_id:
        where += " AND app_id = ?"
        params.append(app_id)
    rows = conn.execute(
        f"""
        SELECT app_id, service, COALESCE(scene, 'unspecified') AS scene, tool,
               COUNT(*) AS requests,
               COUNT(DISTINCT user_id) AS users,
               SUM(outcome = 'ok' AND status < 400) AS succeeded,
               SUM(outcome != 'ok' OR status >= 400) AS failed,
               CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms
        FROM requests WHERE {where}
        GROUP BY app_id, service, scene, tool
        ORDER BY app_id, service, requests DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return {"since": since, "app_id": app_id, "breakdown": [dict(row) for row in rows]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    report = summarize(Settings().usage_db_path, app_id=args.app_id, days=args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
