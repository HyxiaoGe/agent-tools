import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SITE = "__site__"


class LimitError(Exception):
    def __init__(self, code, message, retry_after):
        self.code, self.message, self.retry_after = code, message, retry_after


class QuotaCounter:
    """个人与全站共用一次原子预占；只保存匿名标识，不保存客户端 IP。"""

    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.path = Path(settings.quota_db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS daily ("
                "day TEXT NOT NULL, visitor TEXT NOT NULL, calls INTEGER NOT NULL, images INTEGER NOT NULL,"
                "PRIMARY KEY(day, visitor));"
            )
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('salt', ?)", (secrets.token_hex(32),))
            self.salt = bytes.fromhex(db.execute("SELECT value FROM metadata WHERE name='salt'").fetchone()[0])

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def now(self):
        return self.clock().astimezone(SHANGHAI)

    def identity(self, address):
        return "visitor_" + hmac.new(self.salt, address.encode(), hashlib.sha256).hexdigest()[:32]

    def _rows(self, db, day, identity):
        return {
            visitor: (calls, images)
            for visitor, calls, images in db.execute(
                "SELECT visitor, calls, images FROM daily WHERE day=? AND visitor IN (?, ?)", (day, identity, SITE)
            )
        }

    def _snapshot(self, rows, identity, now):
        personal = rows.get(identity, (0, 0))
        site = rows.get(SITE, (0, 0))

        def count(used, limit):
            return {"limit": limit, "used": used, "remaining": max(0, limit - used)}

        result = {
            "day": now.date().isoformat(),
            "resets_at": datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), SHANGHAI).isoformat(),
            "requests": count(personal[0], self.settings.requests_per_day),
            "images": count(personal[1], self.settings.images_per_day),
            "site_requests": count(site[0], self.settings.site_requests_per_day),
            "site_images": count(site[1], self.settings.site_images_per_day),
        }
        result["can_run"] = result["requests"]["remaining"] > 0 and result["site_requests"]["remaining"] > 0
        result["can_generate"] = (
            result["can_run"] and result["images"]["remaining"] > 0 and result["site_images"]["remaining"] > 0
        )
        return result

    def snapshot(self, identity):
        now = self.now()
        with self.connection() as db:
            return self._snapshot(self._rows(db, now.date().isoformat(), identity), identity, now)

    def reserve(self, identity, *, image=False):
        now = self.now()
        day = now.date().isoformat()
        reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
        with self.connection() as db:
            # 多个访客或进程同时请求时，只能有一个事务读取并更新剩余额度。
            db.execute("BEGIN IMMEDIATE")
            rows = self._rows(db, day, identity)
            status = self._snapshot(rows, identity, now)
            checks = [
                ("site_requests", "全站今日试用次数"),
                ("requests", "你今日的试用次数"),
            ]
            if image:
                checks.extend([("site_images", "全站今日生图次数"), ("images", "你今日的生图次数")])
            for key, label in checks:
                if status[key]["remaining"] == 0:
                    raise LimitError(
                        "daily_limit_exceeded",
                        f"{label}已用完，北京时间零点恢复。",
                        max(1, int((reset - now).total_seconds())),
                    )
            for visitor in (identity, SITE):
                db.execute(
                    "INSERT INTO daily VALUES (?, ?, 1, ?) ON CONFLICT(day, visitor) "
                    "DO UPDATE SET calls=calls+1, images=images+excluded.images",
                    (day, visitor, int(image)),
                )
            db.execute("DELETE FROM daily WHERE day < ?", ((now.date() - timedelta(days=30)).isoformat(),))
            return self._snapshot(self._rows(db, day, identity), identity, now)
