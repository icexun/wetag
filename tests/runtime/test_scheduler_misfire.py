"""covers: TC-013, TC-014, TC-015, TC-016

TC-013 迟到触发按宽限期决定执行或错过（US2-6）
TC-014 重启后合并错过轮次并只补跑一次（US2-7）
TC-015 上轮未结束时跳过新一轮且不补跑（US2-8）
TC-016 存储恢复后仅补跑合格候选轮次（US2-9）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.runtime.jobs.base import Job, JobContext
from app.runtime.scheduler.engine import SchedulerEngine, build_round_idem_key
from app.runtime.state import missed_key, record_missed, record_skipped, skipped_key

GRACE = 300  # 宽限期 5 分钟


class _CronJob(Job):
    kind = TaskKind.DETERMINISTIC
    default_cron = "* * * * *"

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult()


@pytest.fixture()
def engine(task_repo, state_store):
    return SchedulerEngine(
        task_repo.workspace_id,
        task_repo=task_repo,
        state=state_store,
        misfire_grace_seconds=GRACE,
    )


class TestGraceWindow:
    """TC-013: 宽限期内照常执行；超期轮次记录为「错过」，不执行且不积累。"""

    async def test_within_grace_executes(self, engine, task_repo):
        engine.add_job(_CronJob("doc_poll"))
        round_at = datetime.now(UTC) - timedelta(minutes=4)  # 宽限期内
        task = await engine.dispatch("doc_poll", scheduled_at=round_at)
        assert task is not None and task.status == TaskStatus.PENDING
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 1

    async def test_beyond_grace_recorded_missed(self, engine, task_repo, state_store):
        engine.add_job(_CronJob("doc_poll"))
        round_at = datetime.now(UTC) - timedelta(minutes=6)  # 超宽限期
        task = await engine.dispatch("doc_poll", scheduled_at=round_at)
        assert task is None  # 不执行
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0  # 不积累
        missed = await state_store.get(missed_key("doc_poll"))
        assert missed["count"] == 1  # 记录为「错过」可查询


class TestCoalescedRecovery:
    """TC-014: 停机期间多次错过合并为一个候选；最近一次在宽限期内仅补跑一次。"""

    async def test_multiple_misses_coalesce_and_replay_once(
        self, engine, task_repo, state_store
    ):
        engine.add_job(_CronJob("doc_poll"))
        now = datetime.now(UTC)
        for minutes_ago in (3, 2, 1):  # 停机期间错过 3 轮
            await record_missed(state_store, "doc_poll", round_at=now - timedelta(minutes=minutes_ago))
        missed = await state_store.get(missed_key("doc_poll"))
        assert missed["count"] == 3  # 合并为一个候选

        result = await engine.recover_missed("doc_poll")  # 最近一次在宽限期内
        assert result == "replayed"
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 1  # 仅补跑一次
        assert await state_store.get(missed_key("doc_poll")) is None

    async def test_last_miss_beyond_grace_not_replayed(
        self, engine, task_repo, state_store
    ):
        engine.add_job(_CronJob("doc_poll"))
        now = datetime.now(UTC)
        for minutes_ago in (130, 125, 120):  # 停机 2 小时，最近一次也超宽限期
            await record_missed(state_store, "doc_poll", round_at=now - timedelta(minutes=minutes_ago))
        result = await engine.recover_missed("doc_poll")
        assert result == "missed"  # 整体记录「错过」，等待下一正常轮次
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0
        assert (await state_store.get(missed_key("doc_poll")))["count"] == 3

    async def test_job_registered_with_coalesce_and_grace(self, engine):
        engine.add_job(_CronJob("doc_poll"))
        engine.start()
        try:
            job = engine.get_job("doc_poll")
            assert job.coalesce is True  # 多次错过合并（US2-7）
            assert job.max_instances == 1
            assert job.misfire_grace_time == GRACE
        finally:
            engine.shutdown()


class TestSkipWhenRunning:
    """TC-015: 上轮未结束 → 不启动第二个实例，本轮记录为「跳过」，不创建补跑候选。"""

    async def test_max_instances_event_records_skip(self, engine, task_repo, state_store):
        engine.add_job(_CronJob("doc_poll"))
        engine.start()
        try:
            assert engine.get_job("doc_poll").max_instances == 1  # 不启动第二个实例
        finally:
            engine.shutdown()

        round_at = datetime.now(UTC)
        event = SimpleNamespace(
            code=EVENT_JOB_MAX_INSTANCES,
            job_id="doc_poll",
            scheduled_run_time=round_at,
        )
        await engine.handle_scheduler_event(event)

        skipped = await state_store.get(skipped_key("doc_poll"))
        assert skipped["count"] == 1
        assert "上一轮未结束" in skipped["reason"]  # 记录为「因上一轮未结束而跳过」
        # 不创建补跑候选
        assert await state_store.get(missed_key("doc_poll")) is None
        assert await engine.recover_missed("doc_poll") == "none"
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0


class TestRecoveryCandidates:
    """TC-016: 恢复处理仅补跑「可执行」轮次；已跳过、已终止、已取消不补跑。"""

    async def test_only_executable_rounds_replayed(self, engine, task_repo, state_store):
        now = datetime.now(UTC)
        round_at = now - timedelta(minutes=2)  # 宽限期内的候选轮次
        for name in ("job_ok", "job_skipped", "job_dead", "job_cancelled"):
            engine.add_job(_CronJob(name))
            await record_missed(state_store, name, round_at=round_at)

        # 已跳过：同轮次有 skipped 记录
        await record_skipped(state_store, "job_skipped", round_at=round_at)
        # 已终止 / 已取消：同轮次已有 DEAD 任务（DEAD 承载终止与取消，无独立 CANCELLED 态）
        for name, err in (("job_dead", "达最大失败次数终止"), ("job_cancelled", "审批超时取消")):
            spec = TaskSpec(
                job_name=name,
                kind=TaskKind.DETERMINISTIC,
                trigger_source=TriggerSource.SCHEDULE,
                idem_key=build_round_idem_key(task_repo.workspace_id, name, round_at),
            )
            task = await task_repo.create(spec)
            await task_repo.claim(task.id, lease_seconds=60)
            await task_repo.mark_dead(task.id, err)

        assert await engine.recover_missed("job_ok") == "replayed"
        assert await engine.recover_missed("job_skipped") == "skipped"
        assert await engine.recover_missed("job_dead") == "terminal"
        assert await engine.recover_missed("job_cancelled") == "terminal"

        # 仅可执行轮次产生新任务，且只补跑一次
        pending = await task_repo.pending_tasks()
        assert [t.job_name for t in pending] == ["job_ok"]
