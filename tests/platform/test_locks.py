"""T009：并发原语 —— 同群串行锁、仓库锁、模型并发信号量。"""
from __future__ import annotations

import asyncio

from app.platform.concurrency.locks import KeyedLock, llm_semaphore


class TestKeyedLock:
    async def test_same_key_serializes(self):
        locks = KeyedLock()
        events: list[str] = []

        async def worker(name: str):
            async with locks.hold("chat-1"):
                events.append(f"{name}-start")
                await asyncio.sleep(0.02)
                events.append(f"{name}-end")

        await asyncio.gather(worker("a"), worker("b"))
        # 同群两个任务不重叠（US3-6 的串行侧）
        assert events in (
            ["a-start", "a-end", "b-start", "b-end"],
            ["b-start", "b-end", "a-start", "a-end"],
        )

    async def test_different_keys_parallel(self):
        locks = KeyedLock()
        overlap = False
        e1, e2 = asyncio.Event(), asyncio.Event()

        async def w1():
            nonlocal overlap
            async with locks.hold("chat-1"):
                e1.set()
                # 等 w2 进入临界区；w2 能进入即证明跨 key 并行
                await asyncio.wait_for(e2.wait(), timeout=1)
                overlap = True

        async def w2():
            async with locks.hold("chat-2"):
                await asyncio.wait_for(e1.wait(), timeout=1)
                e2.set()

        await asyncio.gather(w1(), w2())
        assert overlap is True  # 跨群并行（US3-6 的并行侧）

    async def test_lock_recycled_after_release(self):
        locks = KeyedLock()
        async with locks.hold("k"):
            assert len(locks) == 1
        assert len(locks) == 0


class TestLlmSemaphore:
    async def test_concurrency_capped(self):
        sem = llm_semaphore(2)
        current = 0
        peak = 0

        async def call():
            nonlocal current, peak
            async with sem:
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.01)
                current -= 1

        # 饱和时排队而非失败（US3-12）：5 个调用全部完成
        await asyncio.gather(*(call() for _ in range(5)))
        assert peak <= 2
