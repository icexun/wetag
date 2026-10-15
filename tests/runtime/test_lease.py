"""covers: TC-022

TC-022 重启回收无执行者任务且保留尝试次数（US3-5）
"""
from __future__ import annotations

import asyncio

import pytest

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.runtime.worker.lease import LeaseKeeper
from app.runtime.worker.pool import WorkerPool
from app.runtime.worker.queue import TaskQueue


def _spec(**kw) -> TaskSpec:
    return TaskSpec(
        job_name="probe", kind=TaskKind.DETERMINISTIC,
        trigger_source=TriggerSource.SCHEDULE, **kw,
    )


class TestLeaseRenew:
    async def test_renew_extends_lease(self, task_repo):
        keeper = LeaseKeeper(task_repo, lease_seconds=60)
        task = await task_repo.create(_spec())
        claimed = await task_repo.claim(task.id, lease_seconds=60)
        before = claimed.lease_expire_at

        assert await keeper.renew(task.id) is True
        after = (await task_repo.get(task.id)).lease_expire_at
        assert after >= before

    async def test_renew_fails_when_not_running(self, task_repo):
        keeper = LeaseKeeper(task_repo, lease_seconds=60)
        task = await task_repo.create(_spec())
        # 未领取（PENDING）时续租无效
        assert await keeper.renew(task.id) is False


class TestReapExpired:
    """TC-022: 崩溃残留的 RUNNING 任务回到 PENDING 且 attempt 保留。"""

    async def test_reap_expired_preserves_attempt(self, task_repo):
        keeper = LeaseKeeper(task_repo)
        task = await task_repo.create(_spec(max_attempt=3))

        # 第一次执行失败：attempt=1，回到 PENDING
        await task_repo.claim(task.id, lease_seconds=60)
        await task_repo.fail(task.id, "boom", backoff_seconds=0)
        # 第二次领取后进程「崩溃」：租约过期残留 RUNNING
        await task_repo.claim(task.id, lease_seconds=-1)

        reaped = await keeper.reap_expired()
        assert [t.id for t in reaped] == [task.id]

        after = await task_repo.get(task.id)
        assert after.status == TaskStatus.PENDING
        assert after.attempt == 1  # 已消耗尝试次数保留、不清零
        assert after.lease_expire_at is None

    async def test_reap_ignores_live_lease(self, task_repo):
        keeper = LeaseKeeper(task_repo)
        task = await task_repo.create(_spec())
        await task_repo.claim(task.id, lease_seconds=600)  # 租约未过期
        assert await keeper.reap_expired() == []
        assert (await task_repo.get(task.id)).status == TaskStatus.RUNNING

    async def test_reaped_task_requeueable(self, task_repo):
        """回收后可重新排队并被领取执行。"""
        keeper = LeaseKeeper(task_repo)
        task = await task_repo.create(_spec())
        await task_repo.claim(task.id, lease_seconds=-1)
        await keeper.reap_expired()

        pending = await task_repo.pending_tasks()
        assert [t.id for t in pending] == [task.id]
        reclaimed = await task_repo.claim(task.id, lease_seconds=60)
        assert reclaimed is not None and reclaimed.status == TaskStatus.RUNNING

    async def test_reaped_task_pushed_to_queue(self, task_repo):
        """TC-022: 回收置 PENDING 后推回内存队列，运行期即可重领，不等重启。"""
        queue = TaskQueue()
        keeper = LeaseKeeper(task_repo, queue=queue)
        task = await task_repo.create(_spec())
        await task_repo.claim(task.id, lease_seconds=-1)

        await keeper.reap_expired()

        assert len(queue) == 1
        popped = await queue.pop(timeout=0.1)
        assert popped.id == task.id
        assert popped.status == TaskStatus.PENDING

    async def test_reaped_task_reclaimed_by_worker(self, task_repo):
        """TC-022: 回收任务经队列被 worker 实际重新领取执行。"""
        queue = TaskQueue()
        keeper = LeaseKeeper(task_repo, queue=queue)
        calls = 0

        async def handler(ctx):
            nonlocal calls
            calls += 1
            return JobResult()

        pool = WorkerPool(
            task_repo, {"probe": handler}, workers=1, queue=queue, renew_interval=3600
        )
        task = await task_repo.create(_spec())
        await task_repo.claim(task.id, lease_seconds=-1)  # 僵尸：租约过期残留 RUNNING
        await keeper.reap_expired()

        run_task = asyncio.create_task(pool.run())
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if (await task_repo.get(task.id)).status == TaskStatus.SUCCEEDED:
                    break
            else:
                pytest.fail("回收任务未被 worker 重新领取执行")
        finally:
            pool.stop()
            await run_task

        assert calls == 1
        final = await task_repo.get(task.id)
        assert final.attempt == 0  # 回收不消耗 attempt
