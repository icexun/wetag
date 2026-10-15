"""TAPD 额度水位的本地估算（US2-10）。

TAPD 没有"查剩余额度"的接口，水位只能由调用方
自己记账：本进程近一个滑窗内对 TAPD 发起的调用次数，对配置上限
（``TAPD_QUOTA_LIMIT``）取余。

收到服务端 ``QUOTA_EXCEEDED`` 时水位直接归零并记住发生点——那是唯一来自
服务端的真信号，必须盖过本地估算；滑窗内的调用老化出去后自动恢复。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

import config


class QuotaMeter:
    """滑窗计数 + 耗尽记忆。两个 TAPD 适配器各持一个，互不共享。"""

    def __init__(
        self,
        *,
        limit: int | None = None,
        window_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = (
            limit if limit is not None else config.get_int("TAPD_QUOTA_LIMIT", 1000)
        )
        self._window = window_s
        self._clock = clock
        self._calls: deque[float] = deque()
        self._exhausted_at: float | None = None

    def record(self) -> None:
        """记一次对 TAPD 的调用（无论成败——失败的调用也占额度）。"""
        now = self._clock()
        self._calls.append(now)
        self._prune(now)

    def note_exhausted(self) -> None:
        """服务端报额度耗尽：水位归零，直到滑窗内的调用全部老化出去。"""
        self._exhausted_at = self._clock()

    def watermark(self) -> dict[str, int]:
        """``{"remaining": …, "limit": …, "used": …, "window_s": …}``。"""
        now = self._clock()
        self._prune(now)
        used = len(self._calls)
        if self._exhausted_at is not None and now - self._exhausted_at < self._window:
            remaining = 0
        else:
            remaining = max(0, self._limit - used)
        return {
            "remaining": remaining,
            "limit": self._limit,
            "used": used,
            "window_s": int(self._window),
        }

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()


__all__ = ["QuotaMeter"]
