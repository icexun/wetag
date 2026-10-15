"""per-chat 双层令牌桶：24/min、800/h，留 20% 余量。

出站四道关卡第 3 关（plan「出站四道关卡」）：
- 普通消息限 OUTBOX_RATE_PER_MINUTE/HOUR（默认 24/min、800/h）
- urgent 消息可透支预留余量至平台硬限（默认 30/min、1000/h），
  保证回执不被普通推送挤占（US4×US5 交互，TC-090）
- 取不到令牌不丢弃，由 Dispatcher 按 retry_after 重排 not_before（US5-6）

桶状态是易失的速率记忆，进程重启后从满桶重新计，不影响正确性；
出站事实（状态/not_before/attempt）全部在 outbox 表（NFR-005）。
"""
from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_RATE_PER_MINUTE = 24
DEFAULT_RATE_PER_HOUR = 800
DEFAULT_URGENT_RATE_PER_MINUTE = 30
DEFAULT_URGENT_RATE_PER_HOUR = 1000


class TokenBucket:
    """令牌桶：capacity 为硬限，refill 按速率线性补充。"""

    def __init__(
        self, capacity: float, refill_per_second: float, clock: Callable[[], float]
    ) -> None:
        self.capacity = float(capacity)
        self.refill = float(refill_per_second)
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def _catch_up(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill)
            self._updated = now

    def available(self) -> float:
        self._catch_up()
        return self._tokens

    def consume(self, n: float = 1.0) -> None:
        self._catch_up()
        self._tokens -= n

    def wait_seconds(self, needed: float) -> float:
        """距攒够 needed 个令牌还需多少秒；已够则为 0。"""
        lack = needed - self.available()
        return 0.0 if lack <= 0 else lack / self.refill


class Throttle:
    """per-chat 分钟桶 + 小时桶。普通消息须为 urgent 预留余量。"""

    def __init__(
        self,
        *,
        per_minute: int = DEFAULT_RATE_PER_MINUTE,
        per_hour: int = DEFAULT_RATE_PER_HOUR,
        urgent_per_minute: int = DEFAULT_URGENT_RATE_PER_MINUTE,
        urgent_per_hour: int = DEFAULT_URGENT_RATE_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.urgent_per_minute = urgent_per_minute
        self.urgent_per_hour = urgent_per_hour
        self._clock = clock
        self._minute_buckets: dict[str, TokenBucket] = {}
        self._hour_buckets: dict[str, TokenBucket] = {}

    def _buckets(self, chat_id: str) -> tuple[TokenBucket, TokenBucket]:
        if chat_id not in self._minute_buckets:
            self._minute_buckets[chat_id] = TokenBucket(
                self.urgent_per_minute, self.urgent_per_minute / 60.0, self._clock
            )
            self._hour_buckets[chat_id] = TokenBucket(
                self.urgent_per_hour, self.urgent_per_hour / 3600.0, self._clock
            )
        return self._minute_buckets[chat_id], self._hour_buckets[chat_id]

    def acquire(self, chat_id: str, *, urgent: bool = False) -> float | None:
        """取一个令牌。返回 None 表示已取到；否则返回距可发时刻的秒数（重排用，US5-6）。

        普通消息取后须保留 urgent 余量（硬限 - 常规限）；urgent 可透支至硬限。
        """
        minute, hour = self._buckets(chat_id)
        reserve_m = self.urgent_per_minute - self.per_minute
        reserve_h = self.urgent_per_hour - self.per_hour
        needed_m = 1.0 if urgent else 1.0 + reserve_m
        needed_h = 1.0 if urgent else 1.0 + reserve_h
        wait = max(minute.wait_seconds(needed_m), hour.wait_seconds(needed_h))
        if wait > 0:
            return wait
        minute.consume()
        hour.consume()
        return None
