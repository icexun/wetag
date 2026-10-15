"""确定性：大仓 git pull（骨架）。

同步执行归 Capability；此处只经 CodeRepositoryPort 记录新鲜度（US3-11 快照时间来源）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind
from app.runtime.jobs.base import Job, JobContext, require_port

if TYPE_CHECKING:
    from app.platform.degradation import FreshnessTracker


class RepoSyncJob(Job):
    """代码仓同步骨架：记录最近一次成功同步时刻，供结论附快照时间（US3-11）。"""

    name = "repo_sync"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 300.0
    default_cron = "*/10 * * * *"

    def __init__(self, repo_port: Any = None, freshness: FreshnessTracker | None = None) -> None:
        self._repo = repo_port
        self._freshness = freshness

    async def run(self, ctx: JobContext) -> JobResult:
        port = self._repo or require_port(ctx, "repo")
        synced_at = await port.last_synced_at()
        if self._freshness is not None:
            await self._freshness.record_success(self.name)
        return JobResult(
            metrics={"last_synced_at": synced_at.isoformat() if synced_at else None}
        )
