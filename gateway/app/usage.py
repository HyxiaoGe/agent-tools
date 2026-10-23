from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    app_id TEXT NOT NULL,
    user_id TEXT,
    scene TEXT,
    request_id TEXT,
    service TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_requests_app ON requests (app_id, ts);
"""


@dataclass(frozen=True)
class UsageRecord:
    ts: str
    app_id: str
    user_id: str | None
    scene: str | None
    request_id: str | None
    service: str
    method: str
    path: str
    status: int
    # ok：上游返回了响应（含 4xx/5xx）；busy / timeout / unreachable：网关自身判定的失败。
    outcome: str
    duration_ms: int


class UsageStore:
    """按请求记一行：谁（产品、用户、场景）调了哪个服务、结果和耗时。"""

    def __init__(self, path: str, retention_days: int):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._retention = timedelta(days=retention_days)
        self._last_prune: datetime | None = None

    def record_sync(self, record: UsageRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests (ts, app_id, user_id, scene, request_id, service, method, path, status, "
                "outcome, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                astuple(record),
            )
            now = datetime.now(UTC)
            if self._last_prune is None or now - self._last_prune > timedelta(days=1):
                self._conn.execute("DELETE FROM requests WHERE ts < ?", ((now - self._retention).isoformat(),))
                self._last_prune = now
            self._conn.commit()

    async def record(self, record: UsageRecord) -> None:
        await asyncio.to_thread(self.record_sync, record)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
