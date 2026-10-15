"""Job 注册表与 cron 配置绑定（JOB_CRON_DOC_POLL 等）。

频率全部走配置不写死（US2-4）：cron_for 的解析顺序为
环境变量/配置中心 JOB_CRON_{NAME} → Job.default_cron。
"""
from __future__ import annotations

import config
from app.runtime.jobs.base import Job, LeaseReaperJob
from app.runtime.jobs.doc_poll import DocPollJob
from app.runtime.jobs.index_refresh import IndexRefreshJob
from app.runtime.jobs.loop_digest import LoopDigestJob
from app.runtime.jobs.repo_sync import RepoSyncJob
from app.runtime.jobs.tapd_sync import TapdSyncJob
from app.runtime.jobs.todo_readback import TodoReadbackJob


def cron_config_key(job_name: str) -> str:
    """job 名 → 配置键：doc_poll → JOB_CRON_DOC_POLL。"""
    return f"JOB_CRON_{job_name.upper()}"


class JobRegistry:
    """Job 名 → Job 实例的注册表；cron 表达式经配置链解析。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def register(self, job: Job) -> Job:
        if not job.name:
            raise ValueError("Job 必须声明 name")
        self._jobs[job.name] = job
        return job

    def unregister(self, name: str) -> Job | None:
        return self._jobs.pop(name, None)

    def get(self, name: str) -> Job:
        return self._jobs[name]

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def cron_for(self, name: str) -> str | None:
        """该 job 的生效 cron：配置覆盖优先，缺省用 Job.default_cron（US2-4）。"""
        job = self._jobs[name]
        return config.get(cron_config_key(name), job.default_cron)

    def scheduled_jobs(self) -> list[Job]:
        """有 cron 的常驻 job（派生型 Agentic Job 无 cron，不常驻调度）。"""
        return [j for j in self._jobs.values() if self.cron_for(j.name)]


def discover_jobs(*, lease_keeper=None, approval_expiry=None) -> JobRegistry:
    """装配八个 Job 的注册表骨架。

    外部 Port 在 container/测试侧注入；lease_reaper 需 LeaseKeeper、
    approval_expiry 需审批/任务仓储，未提供时不注册（由 bootstrap 接线后补注册）。
    """
    registry = JobRegistry()
    registry.register(DocPollJob())
    registry.register(TapdSyncJob())
    registry.register(RepoSyncJob())
    registry.register(TodoReadbackJob())
    registry.register(IndexRefreshJob())
    registry.register(LoopDigestJob())
    if lease_keeper is not None:
        registry.register(LeaseReaperJob(lease_keeper))
    if approval_expiry is not None:
        registry.register(approval_expiry)
    return registry
