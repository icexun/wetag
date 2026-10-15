"""KeyedLock：chat_locks / repo_lock / index_lock，LLM 并发信号量。

- chat_locks：同群串行、跨群并行（US3-6）
- repo_lock / index_lock：按仓库 / 索引互斥
- llm_semaphore：模型并发上限，饱和时任务排队而非失败（US3-12）
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedLock:
    """按 key 懒创建 asyncio.Lock；释放后无等待者即回收，避免无界增长。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, int] = {}

    def __getitem__(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self[key]
        # 先登记再 acquire：保证「有等待者」对持有方可见，避免锁被提前回收
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._holders[key] -= 1
            if self._holders[key] == 0 and not lock.locked():
                del self._holders[key]
                self._locks.pop(key, None)

    def __len__(self) -> int:
        return len(self._locks)


chat_locks = KeyedLock()
repo_lock = KeyedLock()
index_lock = KeyedLock()

from weakref import WeakKeyDictionary

# 按事件循环缓存：Semaphore 首次使用时绑定 loop，跨 loop 复用会抛
# "bound to a different event loop"（pytest 每测试一个 loop 的全量混跑场景）
_llm_semaphores: WeakKeyDictionary = WeakKeyDictionary()
_llm_semaphores_no_loop: dict[int, asyncio.Semaphore] = {}


def llm_semaphore(max_concurrency: int = 4) -> asyncio.Semaphore:
    """模型并发信号量（惰性创建，避免 import 期绑定事件循环）。"""
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        sem = _llm_semaphores_no_loop.get(max_concurrency)
        if sem is None:
            sem = _llm_semaphores_no_loop[max_concurrency] = asyncio.Semaphore(max_concurrency)
        return sem
    per_loop = _llm_semaphores.setdefault(loop, {})
    sem = per_loop.get(max_concurrency)
    if sem is None:
        sem = per_loop[max_concurrency] = asyncio.Semaphore(max_concurrency)
    return sem
