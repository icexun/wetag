"""covers: TC-089

TC-089 缺失外部 Port 时向调用方显式报告未完成（B线）
"""
from __future__ import annotations

import pytest

from app.contracts.errors import PortUnavailable
from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.platform.observability.alerting import AlertKind
from app.runtime.jobs.base import Job, JobContext, LeaseReaperJob
from app.runtime.jobs.index_refresh import IndexRefreshJob
from app.runtime.worker.lease import LeaseKeeper
from app.runtime.worker.pool import WorkerPool


def _spec(job_name: str, **kw) -> TaskSpec:
    return TaskSpec(
        job_name=job_name,
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.DERIVED,
        **kw,
    )


class TestMissingPort:
    """TC-089: Port 未绑定 → 「未完成 + 外部能力不可用」，不返回假成功，留存失败摘要。"""

    async def test_job_raises_port_unavailable(self, task_repo):
        task = await task_repo.create(_spec("index_refresh"))
        ctx = JobContext(task=task, workspace_id=task_repo.workspace_id, extras={})
        with pytest.raises(PortUnavailable, match="外部能力不可用"):
            await IndexRefreshJob().run(ctx)

    async def test_worker_records_honest_failure(self, task_repo, state_store, alerts):
        worker = WorkerPool(
            task_repo,
            {"index_refresh": IndexRefreshJob()},
            state=state_store,
            renew_interval=3600,
        )
        task = await task_repo.create(_spec("index_refresh", max_attempt=1))
        final = await worker.execute(task)
        assert final.status == TaskStatus.DEAD  # 明确失败，而非假成功
        assert "外部能力不可用" in final.error  # 失败摘要留存可查
        assert any(a.kind == AlertKind.FAILURE for a in alerts)

    async def test_worker_injected_ports_reach_job_context(self, task_repo):
        """WorkerPool 接线约定：ports 经 JobContext.extras 注入，Job 内 require_port 可取。"""
        seen: dict = {}

        class _PortJob(Job):
            name = "port_probe"
            kind = TaskKind.DETERMINISTIC

            async def run(self, ctx: JobContext) -> JobResult:
                seen["document"] = ctx.extras.get("document")
                return JobResult()

        worker = WorkerPool(
            task_repo,
            {"port_probe": _PortJob()},
            renew_interval=3600,
            ports={"document": object()},
        )
        task = await task_repo.create(
            TaskSpec(
                job_name="port_probe",
                kind=TaskKind.DETERMINISTIC,
                trigger_source=TriggerSource.SCHEDULE,
            )
        )
        final = await worker.execute(task)
        assert final.status == TaskStatus.SUCCEEDED
        assert seen["document"] is not None


class TestLeaseReaperJob:
    """lease_reaper 骨架：回收僵尸任务，attempt 保留（配合 US3-5）。"""

    async def test_reaps_zombie_tasks(self, task_repo):
        keeper = LeaseKeeper(task_repo)
        job = LeaseReaperJob(keeper)
        zombie = await task_repo.create(
            TaskSpec(
                job_name="probe",
                kind=TaskKind.DETERMINISTIC,
                trigger_source=TriggerSource.SCHEDULE,
            )
        )
        await task_repo.claim(zombie.id, lease_seconds=-1)  # 租约已过期
        ctx = JobContext(task=zombie, workspace_id=task_repo.workspace_id)
        result = await job.run(ctx)
        assert result.metrics["reaped"] == 1
        assert (await task_repo.get(zombie.id)).status == TaskStatus.PENDING
