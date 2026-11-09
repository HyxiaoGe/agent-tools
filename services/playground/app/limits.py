import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.quota import LimitError, QuotaCounter


@dataclass
class Visitor:
    calls: deque = field(default_factory=deque)
    active: bool = False


class Limiter:
    """频率与并发在单进程限制，每日个人与全站次数由 SQLite 持久化。"""

    def __init__(self, settings):
        self.settings = settings
        self.visitors = {}
        self.day = ""
        self.active = 0
        self.counter = QuotaCounter(settings)

    def identity(self, address):
        return self.counter.identity(address)

    @asynccontextmanager
    async def admit(self, identity, image=False):
        now = self.counter.now()
        day = now.date().isoformat()
        if day != self.day:
            # 跨天时保留正在运行的请求，避免同一访客并发穿透。
            cutoff = time.monotonic() - 60
            self.visitors = {
                key: value
                for key, value in self.visitors.items()
                if value.active or (value.calls and value.calls[-1] > cutoff)
            }
            self.day = day
        if identity not in self.visitors:
            if len(self.visitors) >= self.settings.max_visitors:
                raise LimitError("busy", "试用访问较多，请稍后再试。", 60)
            self.visitors[identity] = Visitor()
        visitor = self.visitors[identity]
        tick = time.monotonic()
        while visitor.calls and visitor.calls[0] <= tick - 60:
            visitor.calls.popleft()
        if visitor.active or self.active >= self.settings.max_concurrent:
            raise LimitError("busy", "已有请求正在运行，请等待完成后再试。", 3)
        if len(visitor.calls) >= self.settings.requests_per_minute:
            raise LimitError(
                "rate_limited", "操作有些频繁，请稍后再试。", max(1, int(60 - tick + visitor.calls[0]) + 1)
            )
        visitor.active = True
        self.active += 1
        try:
            quota = await asyncio.to_thread(self.counter.reserve, identity, image=image)
            visitor.calls.append(tick)
            yield quota
        finally:
            visitor.active = False
            self.active -= 1
