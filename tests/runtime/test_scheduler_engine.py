"""covers: TC-008, TC-012

TC-008 无活跃会话时定时任务仍执行并留痕（US2-1）
TC-012 多业务线调度器不领取对方任务（US2-5）
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from apscheduler.triggers.interval import IntervalTrigger

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskStatus, TriggerSource
from app.platform.db.pool import create_pool
from app.runtime.jobs.base import Job, JobContext
from app.runtime.repository import TaskRepository
from app.runtime.scheduler.engine import SchedulerEngine
from app.runtime.worker.pool import WorkerPool
from app.runtime.worker.queue import TaskQueue


class _ProbeJob(Job):
    name = "probe"
    kind = TaskKind.DETERMINISTIC
    default_cron = "* * * * *"

    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(metrics={"progress": "ok"})


class TestScheduledDispatch:
    """TC-008: 群内长期无人 @，已注册定时任务到点仍创建、执行并留痕。"""

    async def test_scheduled_job_runs_without_session(self, pool, task_repo, state_store):
        queue = TaskQueue()
        engine = SchedulerEngine(
            task_repo.workspace_id,
            task_repo=task_repo,
            queue=queue,
            state=state_store,
        )
        engine.add_job(_ProbeJob(), trigger=IntervalTrigger(seconds=1))
        engine.start()
        try:
            task = None
            for _ in range(50):  # 最长等 5s 等到首次到点派发
                rows = await task_repo.pending_tasks()
                if rows:
                    task = rows[0]
                    break
                await asyncio.sleep(0.1)
        finally:
            engine.shutdown()

        assert task is not None, "到点未创建任务"
        assert task.trigger_source == TriggerSource.SCHEDULE
        assert task.priority == 5  # 定时任务优先级低于用户触发
        assert task.chat_id is None  # 不依赖活跃会话

        executed: list = []

        async def handler(ctx: JobContext) -> JobResult:
            executed.append(ctx.task.id)
            return JobResult(metrics={"progress": "done"})

        worker = WorkerPool(
            task_repo, {"probe": handler}, state=state_store, renew_interval=3600
        )
        final = await worker.execute(task)
        assert final.status == TaskStatus.SUCCEEDED
        assert final.finished_at is not None  # 执行结果被记录
        assert executed == [task.id]

    async def test_dispatch_dedup_same_round(self, task_repo):
        """同一触发轮次重复派发由 idem_key 去重，只落一条任务。"""
        engine = SchedulerEngine(task_repo.workspace_id, task_repo=task_repo)
        engine.add_job(_ProbeJob())
        round_at = datetime.now(UTC).replace(second=0, microsecond=0)
        first = await engine.dispatch("probe", scheduled_at=round_at)
        again = await engine.dispatch("probe", scheduled_at=round_at)
        assert first is not None and again is None
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 1


class TestCrossWorkspaceIsolation:
    """TC-012: alpha 与 beta 进程同时运行，各自只在自身 schema 创建和执行任务。"""

    async def test_jobstore_and_dispatch_isolated(self, pg_schema_pair):
        wa, wb = pg_schema_pair
        url_a = wa.admin_dsn.replace("postgresql://", "postgresql+psycopg://")
        url_b = wb.admin_dsn.replace("postgresql://", "postgresql+psycopg://")
        pool_a = await create_pool(wa.admin_dsn, wa.workspace, min_size=1, max_size=2)
        pool_b = await create_pool(wb.admin_dsn, wb.workspace, min_size=1, max_size=2)
        repo_a = TaskRepository(pool_a)
        repo_b = TaskRepository(pool_b)
        engine_a = SchedulerEngine(wa.workspace, task_repo=repo_a, jobstore_url=url_a)
        engine_b = SchedulerEngine(wb.workspace, task_repo=repo_b, jobstore_url=url_b)
        engine_a.add_job(_ProbeJob())
        engine_b.add_job(_ProbeJob())
        engine_a.start()
        engine_b.start()
        try:
            async with pool_a.acquire() as conn:
                # jobstore 落各自 schema（US2-5 的硬隔离）
                assert await conn.fetchval(
                    "SELECT to_regclass($1)", f"{wa.schema}.apscheduler_jobs"
                ) is not None
                jobs_a = await conn.fetchval(
                    f"SELECT count(*) FROM {wa.schema}.apscheduler_jobs"
                )
            async with pool_b.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT to_regclass($1)", f"{wb.schema}.apscheduler_jobs"
                ) is not None
                jobs_b = await conn.fetchval(
                    f"SELECT count(*) FROM {wb.schema}.apscheduler_jobs"
                )
            assert jobs_a == 1 and jobs_b == 1  # 各自只持有自己的 job

            # A 派发只在 A 的 task 表落库，B 不受影响
            task = await engine_a.dispatch("probe")
            assert task is not None and task.workspace_id == wa.workspace
            assert await repo_a.count_by_status(TaskStatus.PENDING) == 1
            assert await repo_b.count_by_status(TaskStatus.PENDING) == 0
        finally:
            engine_a.shutdown()
            engine_b.shutdown()
            await pool_a.close()
            await pool_b.close()
