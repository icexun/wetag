"""covers: TC-029

TC-029 模型并发饱和时任务排队而非失败（US3-12）
另覆盖队列基本语义：优先级排序（US3-1）与启动恢复（US3-5 的重排队）。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.task import (
    PRIORITY_SCHEDULED,
    PRIORITY_USER,
    TaskKind,
    TaskSpec,
    TriggerSource,
)
from app.platform.concurrency.locks import llm_semaphore
from app.runtime.worker.queue import TaskQueue


def _task(task_id, priority, scheduled_at=None) -> "object":
    from app.contracts.task import Task

    return Task(
        id=task_id,
        workspace_id="alpha",
        job_name="probe",
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.MENTION,
        priority=priority,
        scheduled_at=scheduled_at or datetime.now(UTC),
    )


class TestPriorityOrdering:
    async def test_user_task_before_scheduled(self):
        from uuid import uuid4

        q = TaskQueue()
        scheduled = _task(uuid4(), PRIORITY_SCHEDULED)
        user = _task(uuid4(), PRIORITY_USER)
        q.push(scheduled)
        q.push(user)
        assert (await q.pop()).id == user.id
        assert (await q.pop()).id == scheduled.id

    async def test_future_task_waits_until_due(self):
        from uuid import uuid4

        q = TaskQueue()
        future = _task(uuid4(), PRIORITY_USER, datetime.now(UTC) + timedelta(seconds=0.1))
        q.push(future)
        with pytest.raises(TimeoutError):
            await q.pop(timeout=0.02)
        assert (await q.pop(timeout=1)).id == future.id

    async def test_future_head_does_not_block_due_task(self):
        """堆顶高优先级未到期任务（重试退避）不阻塞其后已到期的任务。"""
        from uuid import uuid4

        q = TaskQueue()
        future = _task(uuid4(), PRIORITY_USER, datetime.now(UTC) + timedelta(seconds=60))
        due = _task(uuid4(), PRIORITY_SCHEDULED)
        q.push(future)
        q.push(due)
        assert (await q.pop(timeout=0.1)).id == due.id  # 已到期任务先出队
        with pytest.raises(TimeoutError):
            await q.pop(timeout=0.02)  # 未到期任务仍等待

    async def test_duplicate_push_ignored(self):
        from uuid import uuid4

        q = TaskQueue()
        task = _task(uuid4(), PRIORITY_USER)
        assert q.push(task) is True
        assert q.push(task) is False
        assert len(q) == 1


class TestRestorePending:
    async def test_restore_from_db(self, task_repo):
        q = TaskQueue()
        past = datetime.now(UTC) - timedelta(seconds=1)  # 显式已到期，消除时钟偏移
        specs = [
            TaskSpec(job_name="j1", kind=TaskKind.DETERMINISTIC,
                     trigger_source=TriggerSource.SCHEDULE, priority=PRIORITY_SCHEDULED,
                     scheduled_at=past),
            TaskSpec(job_name="j2", kind=TaskKind.AGENTIC,
                     trigger_source=TriggerSource.MENTION, priority=PRIORITY_USER,
                     scheduled_at=past),
        ]
        for s in specs:
            await task_repo.create(s)
        restored = await q.restore_pending(task_repo)
        assert restored == 2
        # 恢复后仍按优先级出队：用户任务在前
        first = await q.pop()
        assert first.job_name == "j2"


class TestModelConcurrencyQueueing:
    """TC-029: 并发饱和时排队等待，全部完成，无失败。"""

    async def test_saturated_llm_concurrency_queues_not_fails(self):
        from uuid import uuid4

        limit, n_tasks = 2, 6
        sem = llm_semaphore(limit)
        q = TaskQueue()
        for _ in range(n_tasks):
            q.push(_task(uuid4(), PRIORITY_USER))

        current = 0
        peak = 0
        done: list = []
        errors: list = []

        async def consumer():
            nonlocal current, peak
            while len(done) + len(errors) < n_tasks:
                try:
                    task = await q.pop(timeout=0.05)
                except TimeoutError:
                    return
                try:
                    async with sem:  # 饱和时在此排队等待，而非失败
                        current += 1
                        peak = max(peak, current)
                        await asyncio.sleep(0.01)
                        current -= 1
                    done.append(task.id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        await asyncio.gather(*(consumer() for _ in range(3)))
        assert not errors
        assert len(done) == n_tasks
        assert peak <= limit
