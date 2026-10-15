"""APScheduler AsyncIOScheduler + SQLAlchemyJobStore(postgresql)；jobstore 落各自 ws_{workspace} schema（§9.4-1，必须隔离）。

调度器只负责「到点塞任务」，不执行业务逻辑：
- dispatch：构造 TaskSpec(trigger=SCHEDULE, priority=5, idem_key=轮次指纹)，
  ON CONFLICT 去重落库后入队，不等待执行结果
- 每个 job 固定 coalesce=True（多次错过合并为一个候选，US2-7）、
  max_instances=1（上轮未结束则本轮跳过，US2-8）、misfire_grace_time（宽限期，US2-6）
- EVENT_JOB_MISSED / EVENT_JOB_MAX_INSTANCES 事件写入 runtime_state（错过/跳过可查询，不静默）
- recover_missed：重启/存储恢复后对合并候选按宽限期补跑一次（US2-7/US2-9）

轮次精度为分钟（cron 最小粒度）：idem_key = sha256(ws + job + 轮次分钟)。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.contracts.task import PRIORITY_SCHEDULED, Task, TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.runtime.state import missed_key, record_missed, record_skipped, skipped_key

if TYPE_CHECKING:
    from app.runtime.jobs.base import Job
    from app.runtime.repository import TaskRepository
    from app.runtime.scheduler.registry import JobRegistry
    from app.runtime.worker.queue import TaskQueue

logger = logging.getLogger(__name__)

DEFAULT_MISFIRE_GRACE_SECONDS = 300  # 宽限期默认 5 分钟（US2-6）

# 终态与「已取消」（DEAD 承载，data-model 无独立 CANCELLED）一律不补跑（US2-9）
_NON_REPLAYABLE = (TaskStatus.DEAD, TaskStatus.SUCCEEDED)


def build_round_idem_key(workspace_id: str, job_name: str, round_at: datetime) -> str:
    """同一触发轮次的幂等键：跨进程重启稳定，重复派发由唯一索引去重。"""
    round_at = round_at.astimezone(UTC)
    raw = f"sched:{workspace_id}:{job_name}:{round_at.isoformat()}"
    return "sched:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


# 持久化 jobstore 只能序列化「模块级函数 + 可 pickle 参数」，
# 因此 APScheduler 调用的入口是模块级函数，按业务线找回本进程引擎。
_ENGINES: dict[str, "SchedulerEngine"] = {}


async def dispatch_entry(job_name: str, workspace_id: str) -> None:
    """APScheduler 到点入口（可被 SQLAlchemyJobStore 序列化引用）。"""
    engine = _ENGINES.get(workspace_id)
    if engine is None:
        raise RuntimeError(f"业务线 {workspace_id!r} 的调度引擎未在本进程注册")
    await engine.dispatch(job_name)


class SchedulerEngine:
    """常驻调度引擎：APScheduler 到点 → dispatch → task 表 → 优先级队列。"""

    def __init__(
        self,
        workspace_id: str,
        *,
        task_repo: TaskRepository | None = None,
        queue: TaskQueue | None = None,
        state: Any = None,
        jobstore_url: str | None = None,
        misfire_grace_seconds: float = DEFAULT_MISFIRE_GRACE_SECONDS,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self._repo = task_repo
        self._queue = queue
        self._state = state
        self.misfire_grace_seconds = misfire_grace_seconds
        self._jobs: dict[str, Job] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

        if scheduler is not None:
            self._scheduler = scheduler
        else:
            kwargs: dict[str, Any] = {"timezone": UTC}
            if jobstore_url:
                from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

                # jobstore 用独立同步连接池（Decision 3），tableschema 隔离杜绝跨业务线抢 job（US2-5）。
                # 禁止把任何业务查询放进该同步 engine。
                kwargs["jobstores"] = {
                    "default": SQLAlchemyJobStore(
                        url=jobstore_url,
                        tableschema=f"ws_{workspace_id}",
                        engine_options={"pool_size": 2, "pool_pre_ping": True},
                    )
                }
            self._scheduler = AsyncIOScheduler(**kwargs)
        self._scheduler.add_listener(
            self._on_scheduler_event, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
        )
        _ENGINES[workspace_id] = self

    # ---- job 注册 ----

    def add_job(
        self,
        job: Job,
        *,
        cron: str | None = None,
        trigger: Any = None,
    ) -> None:
        """注册常驻 job；cron 优先取入参（注册表配置），缺省用 Job.default_cron。"""
        self._jobs[job.name] = job
        if trigger is None:
            expr = cron or job.default_cron
            if not expr:
                return  # 无 cron 的 job（如派生型 Agentic Job）只登记不调度
            trigger = CronTrigger.from_crontab(expr)
        self._scheduler.add_job(
            dispatch_entry,
            trigger=trigger,
            args=[job.name, self.workspace_id],
            id=job.name,
            coalesce=True,  # 多次错过合并为一个候选（US2-7）
            max_instances=1,  # 上轮未结束则本轮跳过（US2-8）
            misfire_grace_time=self.misfire_grace_seconds,  # 宽限期（US2-6）
            replace_existing=True,
        )

    def register_jobs(self, registry: JobRegistry) -> None:
        """按注册表绑定全部常驻 job，cron 走配置（US2-4）。"""
        for job in registry.scheduled_jobs():
            self.add_job(job, cron=registry.cron_for(job.name))

    def get_job(self, name: str) -> Any:
        return self._scheduler.get_job(name)

    # ---- 生命周期 ----

    def start(self) -> None:
        """在运行中的事件循环内启动（必须在协程里调用）。"""
        self._loop = asyncio.get_running_loop()
        self._scheduler.start()

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    # ---- 派发 ----

    async def dispatch(self, job_name: str, *, scheduled_at: datetime | None = None) -> Task | None:
        """到点塞任务：落库（ON CONFLICT 去重）→ 入队；不等待执行结果。

        scheduled_at 显式给定且已超宽限期时：记录「错过」后不执行、不积累（US2-6）。
        """
        now = datetime.now(UTC)
        if scheduled_at is not None:
            round_at = scheduled_at.astimezone(UTC)
            if now - round_at > timedelta(seconds=self.misfire_grace_seconds):
                if self._state is not None:
                    await record_missed(
                        self._state, job_name,
                        round_at=round_at, reason="超过宽限期，记为错过",
                        workspace_id=self.workspace_id,
                    )
                logger.info("job %s 轮次 %s 超宽限期，记为错过", job_name, round_at)
                return None
        else:
            # 轮次精度取分钟（cron 最小粒度），同分钟重复派发由 idem_key 去重
            round_at = now.replace(second=0, microsecond=0)

        job = self._jobs.get(job_name)
        kind = job.kind if job else TaskKind.DETERMINISTIC
        spec = TaskSpec(
            job_name=job_name,
            kind=kind,
            trigger_source=TriggerSource.SCHEDULE,
            idem_key=build_round_idem_key(self.workspace_id, job_name, round_at),
            priority=PRIORITY_SCHEDULED,
            scheduled_at=round_at,
        )
        if self._repo is None:
            raise RuntimeError("SchedulerEngine 未绑定 task_repo，无法派发")
        task = await self._repo.create(spec)
        if task is not None and self._queue is not None:
            self._queue.push(task)
        return task

    # ---- 错过 / 跳过记录与补跑 ----

    def _on_scheduler_event(self, event: Any) -> None:
        """APScheduler 事件入口（同步回调）：转交事件循环异步处理。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.handle_scheduler_event(event))
        )

    async def handle_scheduler_event(self, event: Any) -> None:
        """错过与跳过写入 runtime_state 供控制台查询，不静默丢弃（US2-6/US2-8）。"""
        if self._state is None:
            return
        job_name = getattr(event, "job_id", None)
        if not job_name:
            return
        round_at = getattr(event, "scheduled_run_time", None) or datetime.now(UTC)
        if round_at.tzinfo is None:
            round_at = round_at.replace(tzinfo=UTC)
        if event.code == EVENT_JOB_MISSED:
            await record_missed(
                self._state, job_name,
                round_at=round_at, reason="触发时刻错过（misfire）",
                workspace_id=self.workspace_id,
            )
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            await record_skipped(
                self._state, job_name,
                round_at=round_at, reason="上一轮未结束，本轮跳过",
                workspace_id=self.workspace_id,
            )

    async def recover_missed(self, job_name: str) -> str:
        """恢复处理：对合并后的错过候选按宽限期判定补跑（US2-7/US2-9）。

        返回 none / skipped / terminal / replayed / missed。
        仅「可执行」轮次补跑；已跳过、已终止、已取消（DEAD）的轮次不补跑。
        """
        if self._state is None:
            return "none"
        missed = await self._state.get(missed_key(job_name))
        if not missed:
            return "none"
        last_round = datetime.fromisoformat(missed["last_round_at"])
        if last_round.tzinfo is None:
            last_round = last_round.replace(tzinfo=UTC)

        # 已记录为「跳过」的轮次不是补跑候选（US2-8：不创建补跑候选）
        skipped = await self._state.get(skipped_key(job_name))
        if skipped and skipped.get("last_round_at") == missed.get("last_round_at"):
            return "skipped"

        # 该轮已有终态任务（已终止/已取消/已完成）→ 不补跑（US2-9）
        if self._repo is not None:
            idem = build_round_idem_key(self.workspace_id, job_name, last_round)
            existing = await self._repo.get_by_idem_key(idem)
            if existing is not None and existing.status in _NON_REPLAYABLE:
                return "terminal"

        now = datetime.now(UTC)
        if now - last_round <= timedelta(seconds=self.misfire_grace_seconds):
            # 合并后仅补跑一次；已有同轮次 PENDING 任务时由 idem_key 去重
            await self.dispatch(job_name, scheduled_at=last_round)
            await self._state.delete(missed_key(job_name))
            return "replayed"
        # 最近一次也超期：整体记录「错过」，等待下一正常轮次（US2-7）
        return "missed"

    async def recover_all(self) -> dict[str, str]:
        """启动恢复：对全部已登记 job 执行错过候选处理。"""
        return {name: await self.recover_missed(name) for name in self._jobs}
