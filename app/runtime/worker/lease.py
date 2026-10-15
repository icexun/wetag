"""30s 续租与僵尸任务回收。

- LeaseKeeper.renew：执行期间周期性续租，仅当任务仍 RUNNING
- reap_expired：把 RUNNING 且租约过期的任务重置为 PENDING 并推回内存队列，attempt 保留不清零（US3-5）
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from app.contracts.errors import error_digest
from app.contracts.task import Task, TaskStatus
from app.platform.observability.alerting import AlertKind, alert
from app.runtime.repository import TaskRepository

if TYPE_CHECKING:
    from app.runtime.worker.queue import TaskQueue

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_RENEW_INTERVAL = 30.0


class LeaseKeeper:
    """单任务续租循环 + 全表僵尸回收。"""

    def __init__(
        self,
        repo: TaskRepository,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        renew_interval: float = DEFAULT_RENEW_INTERVAL,
        queue: TaskQueue | None = None,
    ) -> None:
        self._repo = repo
        self.lease_seconds = lease_seconds
        self.renew_interval = renew_interval
        self._queue = queue

    async def renew(self, task_id: UUID) -> bool:
        """续租一次；任务已不在 RUNNING（被回收/终止）时返回 False。"""
        return await self._repo.renew_lease(task_id, self.lease_seconds)

    async def renew_loop(self, task_id: UUID) -> None:
        """续租协程：每 renew_interval 续一次；续不上（失去所有权）即退出。"""
        try:
            while True:
                await asyncio.sleep(self.renew_interval)
                if not await self.renew(task_id):
                    logger.warning("任务 %s 续租失败（可能已被回收），停止续租", task_id)
                    return
        except asyncio.CancelledError:
            raise

    async def reap_expired(self) -> list[Task]:
        """回收僵尸任务：RUNNING 且租约过期 → PENDING；attempt 保留（US3-5）。

        回收置 PENDING 后推回内存队列，运行期即可被 worker 重领，
        不必等重启经 restore_pending 恢复（TC-022「回到 PENDING 并重新排队」）。
        """
        reaped = await self._repo.reap_expired()
        for task in reaped:
            logger.info("回收僵尸任务 %s（attempt 保留为 %s）", task.id, task.attempt)
            self._push_reaped(task)
        return reaped

    def _push_reaped(self, task: Task) -> None:
        """回收任务推回内存队列；与 WorkerPool._push_retry 同一兜底语义：
        push 失败只告警（任务仍在 DB 的 PENDING 中，重启可恢复），终态不入队，
        队列层按 id 去重。
        """
        if self._queue is None or task.status != TaskStatus.PENDING:
            return
        try:
            self._queue.push(task)
        except Exception as exc:  # noqa: BLE001
            alert(AlertKind.FAILURE, "回收任务入队失败，待重启恢复",
                  error_digest(exc),
                  workspace_id=self._repo.workspace_id,
                  task_id=str(task.id), job_name=task.job_name)
